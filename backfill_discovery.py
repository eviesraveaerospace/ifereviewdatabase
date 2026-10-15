"""
Backfill discovery: find IFE-related videos from 2024 onward that the daily
crawl never saw, within a fixed YouTube API quota budget per run.

Two sources, cheapest and highest-yield first:

1. Channel uploads. Every channel that already has >= MIN_CHANNEL_HITS
   IFE-signal videos in the cache (plus KNOWN_IFE_CHANNELS) gets its uploads
   playlist paged back to --since. playlistItems costs 1 unit per 50 videos
   and reaches arbitrarily deep, so this is how we pick up the reviews that
   show the seatback screen for five minutes but never say "IFE" in the
   title - text search cannot find those, a reviewer's upload list can.
   Channel IDs are resolved once through videos.list on a cached video
   (1 unit per 50) and remembered.

2. Search, sliced by quarter. search.list returns at most 50 results per
   call and costs 100 units, so each curated query is run once per quarter
   window (publishedAfter/publishedBefore) instead of once overall. A busy
   airline query therefore yields up to 50 videos per quarter rather than
   50 in total.

Every candidate goes through the same gate as the daily crawl
(_build_youtube_entry_from_api). Progress is kept in
backfill_discovery_state.json so nightly runs resume where they stopped;
the run ends when --units is spent or the API reports quotaExceeded.

Run:  python backfill_discovery.py                  (default 3000 units)
      python backfill_discovery.py --units 8000
      python backfill_discovery.py --since 2024-01-01 --channels-only
      python backfill_discovery.py --status          (what is left)
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
# Captions are fetched at discovery time, but Whisper is not: a single long
# video would otherwise stall the run for minutes. backfill_transcripts.py
# does that work in the nightly job.
os.environ.setdefault("IFE_NO_WHISPER", "1")
# Nor captions: fetching them one video at a time is what made a 300-upload
# channel take ten minutes. backfill_transcripts.py fills them in nightly.
os.environ.setdefault("IFE_NO_TRANSCRIPT", "1")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from ife_crawler import (IFECrawler, KNOWN_IFE_CHANNELS, CHANNEL_HARD_DROP_RE, channel_hard_drop, YOUTUBE_QUERIES,  # noqa: E402
                         AUTO_DISCOVERY_QUERIES, _CURATED_QUERY_COUNT)
from ife_data_manager import IFEDataManager  # noqa: E402

HERE = Path(__file__).parent
STATE = HERE / "backfill_discovery_state.json"
MIN_CHANNEL_HITS = 3        # IFE-signal videos a channel needs to be enumerated
TRUSTED_CHANNEL_HITS = 10   # at this many, the keyword gate is relaxed like KNOWN_IFE_CHANNELS

COST_SEARCH, COST_PAGE = 100, 1


def _ife_signal(r):
    feats = {k for k in (r.get("ife_features") or {}) if k != "seat"}
    return bool(r.get("ife_system") or r.get("screen_tags") or feats)


def _is_airline_channel(name):
    from ife_crawler import is_airline_channel
    return is_airline_channel(name)


# A reviewer channel's upload list is everything they publish: lounge tours,
# airport walks, landings, miles news, Roblox. Channel-found videos must look
# like a cabin/flight review by title (any language the crawl targets).
_STRONG_RE = re.compile(
    r"business class|first class|economy|premium economy|premium|suite|cabin|seat|review|trip report|"
    r"upper class|club world|club europe|club suite|polaris|delta one|qsuite|the residence|apartment|mint|"
    r"long-?haul|short-?haul|"
    r"flight report|full flight|inflight|in-flight|\bife\b|entertainment|wifi|wi-fi|"
    r"ビジネスクラス|エコノミー|ファーストクラス|プレミアム|搭乗記|機内|商务舱|商務艙|经济舱|經濟艙|头等舱|頭等艙|機上|"
    r"비즈니스|이코노미|일등석|탑승기|hạng thương gia|hạng phổ thông|kelas bisnis|kelas ekonomi|"
    r"ชั้นธุรกิจ|ชั้นประหยัด|درجة|بزنس|क्लास|कक्षा", re.I)
_WEAK_RE = re.compile(r"onboard|on board|flying|flew|fly(?:ing)? (?:on|with)|experience|\bflight\b", re.I)
_HARD_DROP_RE = CHANNEL_HARD_DROP_RE
_SOFT_DROP_RE = re.compile(r"miles?\b|points|status|campaign|sale|マイル|キャンペーン|セール|値上げ|減額|ルール|特典|"
                           r"unboxing|haul|q&a|vlog day|news|joining|announce", re.I)


def channel_title_ok(title):
    t = title or ""
    if channel_hard_drop(t):
        return False
    if _STRONG_RE.search(t):
        return True
    return bool(_WEAK_RE.search(t)) and not _SOFT_DROP_RE.search(t)


def _yt_id(url):
    import re
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})|/shorts/([A-Za-z0-9_-]{11})", url or "")
    return (m.group(1) or m.group(2)) if m else None


def quarters(since: str):
    """[(start_iso, end_iso), ...] quarter windows from `since` to now, newest first."""
    start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    out = []
    y, q = start.year, (start.month - 1) // 3
    while True:
        a = datetime(y, 3 * q + 1, 1, tzinfo=timezone.utc)
        if a > now:
            break
        y2, q2 = (y + 1, 0) if q == 3 else (y, q + 1)
        b = min(datetime(y2, 3 * q2 + 1, 1, tzinfo=timezone.utc), now)
        out.append((a.strftime("%Y-%m-%dT%H:%M:%SZ"), b.strftime("%Y-%m-%dT%H:%M:%SZ")))
        y, q = y2, q2
    return list(reversed(out))


def search_queries():
    """Curated YouTube queries plus the YouTube-relevant auto-discovery ones."""
    qs = list(YOUTUBE_QUERIES[:_CURATED_QUERY_COUNT])
    for q in AUTO_DISCOVERY_QUERIES:
        if "site:" in q:
            continue
        if q not in qs:
            qs.append(q)
    return qs


class Backfill:
    def __init__(self, crawler, dm, since, units):
        self.c, self.dm, self.since, self.budget = crawler, dm, since, units
        self.spent = 0
        self.state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        self.state.setdefault("channel_ids", {})      # channel_title -> UC id (or "" if unresolved)
        self.state.setdefault("channels_done", {})    # channel_title -> since used
        self.state.setdefault("searches_done", [])    # "query|start|end"
        self.existing = dm.known_urls()
        self.added = []
        self.stats = Counter()

    # ── helpers ────────────────────────────────────────────────────────────
    def save_state(self):
        STATE.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")

    def out_of_budget(self, cost):
        return self.spent + cost > self.budget or self.c.quota_exhausted

    def flush(self):
        if not self.added:
            return
        self.dm.reload_from_disk()
        known = self.dm.known_urls()
        fresh = [e for e in self.added if e["url"] not in known]
        self.dm.data["reviews"].extend(fresh)
        self.dm.save_cache()
        self.stats["saved"] += len(fresh)
        self.existing.update(e["url"] for e in fresh)
        self.added = []
        self.save_state()

    def ingest(self, ids, trusted=False, source=""):
        ids = [v for v in ids if f"https://www.youtube.com/watch?v={v}" not in self.existing]
        if not ids:
            return 0
        if self.out_of_budget(COST_PAGE * ((len(ids) + 49) // 50)):
            return 0
        details = self.c._yt_fetch_details(ids)
        self.spent += COST_PAGE * ((len(ids) + 49) // 50)
        n = 0
        for vid in ids:
            item = details.get(vid)
            if not item:
                continue
            url = f"https://www.youtube.com/watch?v={vid}"
            self.existing.add(url)
            # Airline-owned channels are marketing, never reviews: skip outright.
            src_ch = source[8:] if source.startswith("channel:") else ""
            if _is_airline_channel(src_ch) or _is_airline_channel(item.get("snippet", {}).get("channelTitle", "")):
                continue
            if source.startswith("channel:"):
                ctitle = item.get("snippet", {}).get("title", "")
                # Hard drops (lounge, airport walk, Roblox...) apply to trusted channels too.
                if channel_hard_drop(ctitle) or (not trusted and not channel_title_ok(ctitle)):
                    continue
            entry = self.c._build_youtube_entry_from_api(vid, item, trusted=trusted)
            if entry:
                entry["discovered_by"] = source
                self.added.append(entry)
                n += 1
        self.stats["candidates"] += len(ids)
        self.stats["accepted"] += n
        return n

    # ── 1. channel uploads ─────────────────────────────────────────────────
    def channel_candidates(self):
        rows = [r for r in self.dm.data.get("reviews", []) if "youtube.com" in (r.get("url") or "")]
        hits = Counter(r.get("channel_title") for r in rows if _ife_signal(r) and r.get("channel_title"))
        sample = {}
        for r in rows:
            ch = r.get("channel_title")
            if ch and ch not in sample and _yt_id(r.get("url")):
                sample[ch] = _yt_id(r.get("url"))
        cands = []
        for name, cid in KNOWN_IFE_CHANNELS.items():
            cands.append((name, cid, True, None))
        for ch, n in hits.most_common():
            if n < MIN_CHANNEL_HITS or ch in KNOWN_IFE_CHANNELS:
                continue
            cands.append((ch, self.state["channel_ids"].get(ch), n >= TRUSTED_CHANNEL_HITS, sample.get(ch)))
        return cands

    def resolve_channel_ids(self, cands):
        """videos.list on one cached video per unresolved channel -> snippet.channelId."""
        todo = [(ch, vid) for ch, cid, _, vid in cands if cid is None and vid]
        for i in range(0, len(todo), 50):
            batch = todo[i:i + 50]
            if self.out_of_budget(COST_PAGE):
                return
            resp = self.c.session.get("https://www.googleapis.com/youtube/v3/videos",
                                      params={"part": "snippet", "id": ",".join(v for _, v in batch),
                                              "key": self.c.api_key},
                                      timeout=15, verify=self.c.verify_ssl)
            self.spent += COST_PAGE
            if resp.status_code != 200:
                self.c._note_api_error(resp, None, "resolve channel ids")
                return
            by_vid = {it["id"]: it["snippet"].get("channelId", "") for it in resp.json().get("items", [])}
            for ch, vid in batch:
                self.state["channel_ids"][ch] = by_vid.get(vid, "")
        self.save_state()

    def run_channels(self):
        cands = self.channel_candidates()
        self.resolve_channel_ids(cands)
        pending = [(ch, self.state["channel_ids"].get(ch) if cid is None else cid, trusted)
                   for ch, cid, trusted, _ in cands
                   if self.state["channels_done"].get(ch) != self.since]
        pending = [(ch, cid, t) for ch, cid, t in pending if cid]
        print(f"Channels to enumerate: {len(pending)} (since {self.since})")
        for n, (ch, cid, trusted) in enumerate(pending, 1):
            if self.out_of_budget(COST_PAGE * 4):
                print("  budget reached during channel pass")
                break
            calls_before = self.c.api_calls
            ids = self.c._yt_search_channel(cid, limit=5000, published_after=self.since)
            self.spent += COST_PAGE * max(1, (len(ids) + 49) // 50)
            if self.c.quota_exhausted:
                break
            got = self.ingest(ids, trusted=trusted, source=f"channel:{ch}")
            self.state["channels_done"][ch] = self.since
            self.stats["channels"] += 1
            print(f"  [{n}/{len(pending)}] {ch[:40]:40s} uploads {len(ids):4d}  new {got:3d}")
            if n % 10 == 0:
                self.flush()
        self.flush()

    # ── 2. quarter-sliced search ───────────────────────────────────────────
    def run_search(self):
        done = set(self.state["searches_done"])
        plan = [(q, a, b) for a, b in quarters(self.since) for q in search_queries()
                if f"{q}|{a}|{b}" not in done]
        print(f"Search calls left: {len(plan)}  (budget allows {max(0, (self.budget - self.spent) // COST_SEARCH)})")
        for n, (q, a, b) in enumerate(plan, 1):
            if self.out_of_budget(COST_SEARCH + COST_PAGE):
                print("  budget reached during search pass")
                break
            ids = self.c._yt_search_api(q, limit=50, published_after=a, published_before=b)
            self.spent += COST_SEARCH
            if self.c.quota_exhausted:
                break
            got = self.ingest(ids, source=f"search:{q}")
            self.state["searches_done"].append(f"{q}|{a}|{b}")
            self.stats["searches"] += 1
            print(f"  [{n}/{len(plan)}] {a[:7]}  {q[:50]:50s} results {len(ids):2d}  new {got:2d}")
            if n % 10 == 0:
                self.flush()
        self.flush()

    def status(self):
        cands = self.channel_candidates()
        ch_left = sum(1 for ch, _, _, _ in cands if self.state["channels_done"].get(ch) != self.since)
        done = set(self.state["searches_done"])
        s_left = sum(1 for a, b in quarters(self.since) for q in search_queries() if f"{q}|{a}|{b}" not in done)
        print(f"since {self.since}: channels left {ch_left}/{len(cands)}, "
              f"search calls left {s_left} (~{s_left * COST_SEARCH:,} units)")


def refilter(dry_run):
    """One-off: drop channel-found cache entries that fail channel_title_ok and
    carry no IFE evidence. Copies go to pruned_offtopic.json."""
    cache = HERE / "ife_cache.json"
    pruned = HERE / "pruned_offtopic.json"
    data = json.loads(cache.read_text(encoding="utf-8"))
    rows = data["reviews"]
    # Same trust rule as the backfill: known reviewers plus channels with many IFE hits.
    hits = Counter(r.get("channel_title") for r in rows if _ife_signal(r) and r.get("channel_title"))
    trusted = set(KNOWN_IFE_CHANNELS) | {ch for ch, n in hits.items() if n >= TRUSTED_CHANNEL_HITS}

    def evidence(r):
        # entertainment_system was set by a substring "ife" match ("life", "wife")
        # on descriptions, so alone it proves nothing; other features, a named
        # system, or an OCR'd screen do.
        feats = {k for k in (r.get("ife_features") or {}) if k not in ("seat", "entertainment_system")}
        return bool(r.get("ife_system") or r.get("screen_tags") or feats)

    def bad(r):
        src = r.get("discovered_by") or ""
        if not src.startswith("channel:"):
            return False
        t = r.get("title") or ""
        if channel_hard_drop(t):
            return True
        return src[8:] not in trusted and not channel_title_ok(t) and not evidence(r)

    gone = [r for r in rows if bad(r)]
    print(f"{len(gone)} of {len(rows)} would be dropped")
    for r in gone[:25]:
        print("  ", (r.get("title") or "")[:90])
    if dry_run:
        return
    urls = {r["url"] for r in gone}
    data["reviews"] = [r for r in rows if r["url"] not in urls]
    old = json.loads(pruned.read_text(encoding="utf-8")) if pruned.exists() else []
    pruned.write_text(json.dumps(old + gone, indent=1, ensure_ascii=False), encoding="utf-8")
    cache.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"dropped {len(gone)}; {len(data['reviews'])} remain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=os.environ.get("BACKFILL_SINCE", "2024-01-01"))
    ap.add_argument("--units", type=int, default=int(os.environ.get("BACKFILL_UNITS", "3000")),
                    help="YouTube API quota units this run may spend")
    ap.add_argument("--channels-only", action="store_true")
    ap.add_argument("--search-only", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--refilter", action="store_true",
                    help="re-apply channel_title_ok to cached channel-found entries with no IFE evidence")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.refilter:
        refilter(a.dry_run)
        return

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        print("YOUTUBE_API_KEY not set - nothing to do")
        return
    dm = IFEDataManager()
    dm.reload_from_disk()
    bf = Backfill(IFECrawler(verify_ssl=False, api_key=api_key), dm, a.since, a.units)
    if a.status:
        bf.status()
        return
    t0 = datetime.now()
    if not a.search_only:
        bf.run_channels()
    if not a.channels_only:
        bf.run_search()
    s = bf.stats
    print(f"\nBackfill done in {(datetime.now() - t0).seconds // 60} min: units {bf.spent}/{a.units}, "
          f"channels {s['channels']}, searches {s['searches']}, candidates {s['candidates']}, "
          f"accepted {s['accepted']}, saved {s['saved']}"
          + ("  ** quota exhausted **" if bf.c.quota_exhausted else ""))
    bf.status()


if __name__ == "__main__":
    main()
