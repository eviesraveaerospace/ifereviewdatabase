"""Backfill metadata the older crawls never stored, straight from the YouTube
Data API and the article pages:

  * published_at / year   — 300+ videos were scraped without a publish date and
                            fell back to a hard-coded year (2025), so real 2026
                            uploads showed as 2025. Now taken from the API.
  * duration_seconds      — needed to recognise Shorts.
  * is_short              — Shorts URL / #shorts tag / <= 3 min.
  * airlines_mentioned    — re-tagged so alias spellings ("Iceland Air") count
                            toward the canonical carrier (Icelandair).
  * article_text          — body paragraphs of press articles for inline display.

Run:  python backfill_video_meta.py            (all of the above)
      python backfill_video_meta.py --dry-run  (report only, no write)
Quota: 1 unit per 50 videos (videos.list) — ~150 units for the whole cache.
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import ife_crawler as ic

DRY = "--dry-run" in sys.argv
CACHE = Path("ife_cache.json")

api_key = os.environ.get("YOUTUBE_API_KEY", "")
if not api_key:
    print("ERROR: YOUTUBE_API_KEY not set (add it to .env)")
    sys.exit(1)

crawler = ic.IFECrawler(verify_ssl=False, api_key=api_key)
data = json.loads(CACHE.read_text(encoding="utf-8"))
rows = data["reviews"]

videos = [r for r in rows if r.get("media_type") == "video"]
by_id = {}
for r in videos:
    vid = crawler._extract_yt_id(r.get("url", ""))
    if vid:
        by_id.setdefault(vid, []).append(r)

print(f"{len(rows)} rows, {len(videos)} videos, {len(by_id)} unique YouTube ids")

# ── 1. YouTube API: dates, durations, Shorts ────────────────────────────────
details = crawler._yt_fetch_details(list(by_id))
print(f"API returned details for {len(details)} videos "
      f"({len(by_id) - len(details)} unavailable/removed)")

dated, redated, year_fixed, shorts = 0, 0, 0, 0
for vid, item in details.items():
    snippet = item.get("snippet", {})
    published = snippet.get("publishedAt", "")
    secs = ic._iso_duration_seconds(item.get("contentDetails", {}).get("duration", ""))
    for r in by_id[vid]:
        if published and not r.get("published_at"):
            r["published_at"] = published
            dated += 1
        elif published and r.get("published_at") != published:
            r["published_at"] = published
            redated += 1
        if published[:4].isdigit() and r.get("year") != int(published[:4]):
            r["year"] = int(published[:4])
            year_fixed += 1
        if secs is not None:
            r["duration_seconds"] = secs
        short = ic._is_short(r.get("title", ""), secs, r.get("url", ""))
        if short:
            shorts += 1
        r["is_short"] = short

# Videos the API no longer knows: keep whatever date exists; if the year was
# only ever the old hard-coded fallback and the title names no year, clear it.
unknown = 0
for vid, rs in by_id.items():
    if vid in details:
        continue
    for r in rs:
        r.setdefault("is_short", ic._is_short(r.get("title", ""), None, r.get("url", "")))
        if not r.get("published_at") and r.get("year") == 2025 \
                and not crawler._year_from_text(r.get("title", "")):
            r["year"] = None
            unknown += 1

print(f"published_at added: {dated}, corrected: {redated}; year corrected: {year_fixed}; "
      f"Shorts flagged: {shorts}; undated fallback-2025 cleared: {unknown}")

# ── 2. Alias re-tagging (Iceland Air → icelandair) ─────────────────────────
retagged = 0
for r in rows:
    if r.get("media_type") == "internal":
        continue
    text = " ".join([
        r.get("title") or "", r.get("transcript_full") or "",
        r.get("transcript_excerpt") or "",
    ])
    hits = ic._keyword_hits(text.lower(), ic.AIRLINE_KEYWORDS)
    have = {a.get("keyword") for a in r.get("airlines_mentioned") or []}
    if set(hits) - have:
        r["airlines_mentioned"] = crawler._mentions(text.lower(), ic.AIRLINE_KEYWORDS)
        retagged += 1
print(f"airline tags refreshed on {retagged} rows")

# ── 3. Article body text ───────────────────────────────────────────────────
arts = [r for r in rows if r.get("media_type") == "article" and not r.get("article_text")]
filled, failed = 0, 0
for r in arts:
    fresh = crawler._fetch_article(r["url"])
    if fresh and fresh.get("article_text"):
        r["article_text"] = fresh["article_text"]
        if fresh.get("year") and not r.get("year"):
            r["year"] = fresh["year"]
        filled += 1
    else:
        failed += 1
print(f"article_text filled: {filled}, unavailable: {failed} (of {len(arts)})")

if DRY:
    print("dry run — nothing written")
else:
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print("ife_cache.json written")
