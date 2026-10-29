"""
Generate chapters for YouTube Shorts from what is ON SCREEN, not from the
description (Shorts almost never carry timestamped descriptions: 10 of 610 do).

Each Short is downloaded at low resolution, sampled every two seconds, and every
frame is read with RapidOCR and tagged with the ios-screen-recording-ocr
project's screen-tag taxonomy (roi_ocr.infer_tags — the same detection used
for employee submissions). Consecutive frames showing the same screen merge
into one chapter, so the dashboard can jump to "Map", "Media player", etc.

Stores on each review:
  chapters        = [{"t":"0:12","sec":12,"end":19,"title":"Media Navigation",
                      "tags":["Media Navigation"],"ife":true,"source":"ocr"}]
  chapters_source = "ocr"
  screen_tags     = union of tags seen (same vocabulary as internal reviews)
  ife_features    = existing + dashboard equivalents of the screen tags

Run:  python gather_short_chapters.py [--limit N] [--url URL] [--retag]
                                      [--max-runtime-min M] [--interval S]
      (--url accepts any cached video, Short or not)
Needs the OCR checkout (IFE_OCR_REPO, or one of the known paths below) with
rapidocr_onnxruntime + opencv installed in this interpreter, and yt-dlp.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

# Must precede the first cv2 import (see ocr_video_chapters.py).
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
CACHE = HERE / "ife_cache.json"
COOKIES = HERE / "cookies.txt"

_OCR_REPO_CANDIDATES = [
    os.environ.get("IFE_OCR_REPO", ""),
    str(Path.home() / "OneDrive - SAFRAN PASSENGER INNOVATIONS" / "Internship - 2026" / "ios-screen-recording-ocr"),
    "C:/workspace/ios-screen-recording-ocr",
    str(Path.home() / "ife"),
]


def _ocr_repo() -> Path:
    for c in _OCR_REPO_CANDIDATES:
        if c and (Path(c) / "ocr_video_chapters.py").exists():
            return Path(c)
    raise SystemExit("ios-screen-recording-ocr checkout not found; set IFE_OCR_REPO")


REPO = _ocr_repo()
sys.path.insert(0, str(REPO))

from ocr_video_chapters import analyze_frames  # noqa: E402
from llm_video_chapters import sample_video     # noqa: E402
from manual_review_tagger import to_ife_features  # noqa: E402
from ocr_results import OWN, apply_results, cache_lock, load_results, save_results  # noqa: E402

# Tags that are still an IFE screen but say nothing a reviewer would jump to.
_QUIET_TAGS = {"Welcome screen"}


def _is_short(r) -> bool:
    return r.get("media_type") == "video" and bool(r.get("is_short") or "/shorts/" in (r.get("url") or ""))


_IFE_TITLE_RE = re.compile(
    r"\b(review|trip report|flight report|business class|first class|economy|premium economy|"
    r"cabin|seat|ife|in-?flight entertainment|entertainment|screen|wifi|wi-fi)\b", re.I)
_OFFTOPIC_TITLE_RE = re.compile(
    r"\b(wwe|wrestl|match|meeting|council|authority|defen[cs]e|war\b|missile|petrol|"
    r"status|points|miles|credit card|rewards|book(ing)? (flight )?tickets?|kaise)\b", re.I)


def _long_priority(r):
    """Rank full-length videos for the OCR budget: IFE review content first.
    Higher is better. Mirrors the dashboard's relevance signals (transcript,
    IFE features, system, airlines) plus title cues; 'seat' alone is too
    common to count as an IFE signal."""
    feats = {k for k in (r.get("ife_features") or {}) if k != "seat"}
    score = 0.0
    if r.get("transcript_available"):
        score += 3.0
    score += min(len(feats), 3) * 1.0
    if r.get("ife_system"):
        score += 2.0
    elif r.get("ife_system_guess"):
        score += 0.5
    score += min(len(r.get("airlines_mentioned") or []), 2) * 0.75
    if r.get("source_tier") == 1:
        score += 1.0
    title = r.get("title") or ""
    score += min(len(_IFE_TITLE_RE.findall(title)), 2) * 0.75
    if _OFFTOPIC_TITLE_RE.search(title):
        score -= 3.0
    if not feats and not r.get("airlines_mentioned") and not r.get("ife_system"):
        score -= 2.0
    return score


def _yt_id(url: str):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def fmt_ts(sec: float) -> str:
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ── download ─────────────────────────────────────────────────────────────────
class _SilentLogger:
    def __init__(self): self.errors = []
    def debug(self, m): pass
    def warning(self, m): pass
    def error(self, m): self.errors.append(m.lower())


_SKIP_SIGNALS = ("private video", "video unavailable", "has been removed", "account has been terminated")
_BLOCK_SIGNALS = ("sign in to confirm", "not a bot", "429", "too many requests", "http error 403")


def _cookies_from_env():
    """The VM keeps YouTube cookies as YOUTUBE_COOKIES_B64 in .env (same as
    backfill_transcripts); materialize them if there is no cookies.txt."""
    if COOKIES.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if b64:
        import base64
        COOKIES.write_bytes(base64.b64decode(b64))


def download(video_id: str, workdir: str, ranges=None):
    """→ (path | None, status) where status ∈ ok / skip / block / fail.
    ranges=[(start_sec, end_sec)] downloads only those sections (ffmpeg cut at
    keyframes), so tagging one IFE chapter costs seconds, not the whole video."""
    import yt_dlp
    logger = _SilentLogger()
    opts = {
        "format": "bv*[height<=480][ext=mp4]/b[height<=480]/b",
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "quiet": True, "no_warnings": True, "nocheckcertificate": True, "logger": logger,
        "extractor_args": {"youtube": {"player_client": ["tv", "android", "web_safari"]}},
    }
    if ranges:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(None, [(float(a), float(b)) for a, b in ranges])
        opts["force_keyframes_at_cuts"] = True
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception as exc:  # noqa: BLE001
        logger.errors.append(str(exc).lower())
    err = " ".join(logger.errors)
    files = [p for p in Path(workdir).iterdir() if p.stem == video_id]
    if files:
        return str(files[0]), "ok"
    if any(s in err for s in _SKIP_SIGNALS):
        return None, "skip"
    if any(s in err for s in _BLOCK_SIGNALS):
        return None, "block"
    return None, "fail"


# ── OCR → chapters ───────────────────────────────────────────────────────────
def ocr_chapters(video_path: str, ocr, interval: float, out_dir: str, long_form: bool = False):
    frames = sample_video(video_path, interval=interval, dedupe_threshold=6.0, max_frames=0, max_width=720)
    args = SimpleNamespace(similarity_threshold=0.5, max_width=720)
    analyze_frames(frames, ocr, args, out_dir)
    raw = json.loads(Path(out_dir, "chapters.json").read_text(encoding="utf-8"))
    return to_dashboard(raw, long_form=long_form)


# Untagged Shorts chapters keep only on-screen titles that read like an IFE
# screen. Everything else OCR picks up in a Short (creator caption overlays,
# channel names, menu dish names, garbled text) is noise in the modal and in
# the Screens index.
_SCREEN_VOCAB = re.compile(
    r"\b(movies?|films?|tv|series|shows?|music|audio|games?|maps?|flight ?info\w*|wifi|wi-?fi|internet|"
    r"connect\w*|menu|entertainment|home|welcome|information|settings?|language|search|seat|seatback|"
    r"screen|display|remote|headphones?|usb|bluetooth|camera|live ?tv|news|sports?|kids|children|"
    r"magazines?|shop|duty ?free|meals?|food|drinks?|dining|beverages?|order|chat|messag\w*|destination|"
    r"arrival|departure|boarding|safety|charge|power|my ?flight|playlists?|podcasts?|box ?sets?|"
    r"new ?releases?|classics?|comedy|drama|action|documentar\w*|continue watching|watch|play|pause|"
    r"subtitles?|volume|brightness|exit|back)\b", re.I)


def to_dashboard(raw_chapters, long_form=False, gap=8):
    """OCR chapters → dashboard rows. Adjacent chapters with the same tag set
    merge; untagged, untitled stretches (video playback, transitions) are
    dropped unless they are the only thing in the clip.

    long_form (full-length reviews rather than Shorts): only tagged IFE
    screens become chapters — raw on-screen text ("RICECOOKIE", a baggage
    chart) is noise at that length — and same-tag runs separated by up to
    `gap` seconds of untagged frames merge into one chapter."""
    rows = []
    for ch in raw_chapters:
        tags = [t for t in ch.get("tags") or [] if t not in _QUIET_TAGS]
        title = " · ".join(tags) if tags else (ch.get("title") or "").strip()
        if not tags and (long_form or not title or title == "Untitled screen" or not re.search(r"[A-Za-z]{3}", title)):
            continue  # untagged and unreadable ("7/211134km") — not a chapter
        if not tags and not _SCREEN_VOCAB.search(title):
            continue  # untagged and not an IFE screen word ("DCTEATS", "SUBSCRIBE")
        if rows and rows[-1]["tags"] == tags and (tags or rows[-1]["title"] == title)                 and ch["start"] - rows[-1]["end"] <= (gap if long_form else 0.01):
            rows[-1]["end"] = ch["end"]
            continue
        rows.append({"t": fmt_ts(ch["start"]), "sec": int(round(ch["start"])), "end": int(round(ch["end"])),
                     "title": title[:90], "tags": tags, "ife": bool(tags), "source": "ocr"})
    if long_form:
        # "Navigation Menu" is the OCR taxonomy's catch-all: any frame whose text
        # hits several tag groups collapses to it, so a lounge sign full of
        # airline logos or a boarding pass reads as one for a single frame. A
        # real hub screen stays up across sampled frames; specific screens
        # (Map, Games, Media player) are kept even from one frame.
        rows = [r for r in rows if r["end"] > r["sec"] or r["tags"] != ["Navigation Menu"]]
    return rows


# ── IFE chapter → tag detections ─────────────────────────────────────────────
IFE_SPAN_MAX = 5 * 60      # cap per chapter: the IFE talk is in the first minutes
TAG_CONF_MIN = 0.8         # a screen seen in one sampled frame only is not shown


# Chapters that are not flagged IFE but usually show the screen: takeoff/landing
# (tail and belly cameras, the map) and menu chapters (the meal menu on the IFE).
# Keep in step with OCR_CAND_RE in templates/index.html.
EXTRA_CHAPTER_RE = re.compile(r"\b(?:take[\s-]?off|landing|menu)\b", re.I)

# Version of everything that decides what the OCR passes store: which chapters are
# read, the sampling interval, and the tagger in ios-screen-recording-ocr. Every row
# an OCR pass writes is stamped with it (ocr_tagger_v); a row stamped lower, or never
# stamped, is read again after the never-tried ones. BUMP THIS whenever tagging
# improves, so earlier results are redone instead of keeping their old mistakes.
#   1 = IFE-flagged chapters only   2 = + takeoff/landing/menu chapters
#   3 = glue/typo-tolerant keywords, title/tab-aware Games, km/mi map readout,
#       paper-menu gate, 4 s (VM) / 2 s (laptop) sampling
TAGGER_V = 3


def _stale(r):
    return (r.get("ocr_tagger_v") or 0) < TAGGER_V


def _ocr_screen_tags(r):
    """screen_tags rebuilt from what the OCR passes found, so a mistake from an
    older tagger is dropped on re-run instead of being merged back in. Rows whose
    tags came from elsewhere ('auto') keep them."""
    tags = {d["tag"] for d in r.get("ife_tags") or []}
    if r.get("chapters_source") == "ocr":
        tags |= {t for c in r.get("chapters") or [] for t in c.get("tags") or []}
    if r.get("screen_tag_source") not in (None, "ocr-ife-chapter", "ocr-video"):
        tags |= set(r.get("screen_tags") or [])
    return sorted(tags)


def ife_spans(r, extra=True):
    """[(chapter_index, start, end)] for each chapter flagged IFE, plus (extra=True)
    takeoff/landing/menu chapters, which typically show part of the IFE."""
    chs = r.get("chapters") or []
    out = []
    for i, c in enumerate(chs):
        if c.get("sec") is None:
            continue
        if not (c.get("ife") or (extra and EXTRA_CHAPTER_RE.search(c.get("title") or ""))):
            continue
        nxt = chs[i + 1]["sec"] if i + 1 < len(chs) and chs[i + 1].get("sec") is not None else None
        if c.get("end"):            # transcript-derived chapters carry their own end
            nxt = c["end"] if nxt is None else min(nxt, c["end"])
        end = min(nxt if nxt is not None else c["sec"] + IFE_SPAN_MAX, c["sec"] + IFE_SPAN_MAX)
        if end > c["sec"]:
            out.append((i, c["sec"], end))
    return out


def _never_tried(r):
    """No OCR pass has read this video yet, and one of them would: IFE chapters for a
    chaptered video, the whole video for one without chapters (Shorts and long)."""
    if r.get("chapters_ocr_status") or r.get("ife_tags_status") or r.get("chapters_source") == "ocr":
        return False
    if r.get("chapters_source") in ("description", "transcript"):
        return bool(ife_spans(r))
    return not r.get("chapters")


def _tag_only(r):
    """A chaptered video with no IFE-looking chapter: no pass reads it, so it has no
    tags. Its description chapters stay; whole-video OCR adds the screen tags only."""
    return (r.get("chapters_source") in ("description", "transcript") and bool(r.get("chapters"))
            and not ife_spans(r) and not (r.get("screen_tags") or r.get("ife_tags"))
            and not (r.get("chapters_ocr_status") or r.get("ife_tags_status")))


def _needs_ife_ocr(r):
    """Not yet tagged, or tagged by an older tagger version. A failure is not retried."""
    st = r.get("ife_tags_status")
    if not st:
        return True
    return st in ("done", "none") and _stale(r)


def tag_detections(rows, offset, chapter_idx, interval):
    """OCR rows of one chapter clip → one detection per tag run, offset to the
    video's own clock. Confidence is persistence: a screen held across two or
    more sampled frames is solid; a single frame is dropped."""
    out = []
    for row in rows:
        # Held across frames: solid. One frame: fine for a specific screen,
        # not for the catch-all "Navigation Menu" (see to_dashboard).
        held = row["end"] - row["sec"] >= interval
        conf = 0.9 if held else (0.6 if row["tags"] == ["Navigation Menu"] else 0.8)
        if conf < TAG_CONF_MIN:
            continue
        for tag in row["tags"]:
            sec = int(round(row["sec"] + offset))
            out.append({"tag": tag, "t": fmt_ts(sec), "sec": sec, "end": int(round(row["end"] + offset)),
                        "conf": conf, "chapter": chapter_idx, "source": "ocr"})
    return out


def _cache_lock(path=None):
    return cache_lock(path or CACHE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N videos (0 = all)")
    ap.add_argument("--url", help="only this video URL")
    ap.add_argument("--retag", action="store_true", help="redo Shorts that already have OCR chapters")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between sampled frames")
    ap.add_argument("--max-runtime-min", type=float, default=float(os.environ.get("MAX_RUNTIME_MIN", 0) or 0))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--shard", default="", help="K/N: process every Nth video starting at K (parallel workers)")
    ap.add_argument("--long", action="store_true",
                    help="full-length videos (not Shorts) that have no chapters at all, newest first")
    ap.add_argument("--oldest-first", action="store_true",
                    help="with --ife-chapters: start from the oldest videos, so a second machine "
                         "does not pick the same ones as the newest-first nightly pass")
    ap.add_argument("--tag-chaptered", action="store_true",
                    help="videos with description chapters but none that look like IFE, and no tags: "
                         "OCR the whole video for screen tags only (the chapters are kept)")
    ap.add_argument("--ife-chapters", action="store_true",
                    help="videos with a description chapter flagged IFE: OCR just that chapter and "
                         "store its screen-tag detections as ife_tags (newest first)")
    ap.add_argument("--new", action="store_true",
                    help="every video published in the last --since-days (or held at admission until it has "
                         "chapters and tags) that no OCR pass has tried, "
                         "whatever its kind (Short, chaptered, long), newest first: run first each night "
                         "so a new video gets its detections ahead of the backlog")
    ap.add_argument("--since-days", type=int, default=14)
    ap.add_argument("--reverse", action="store_true",
                    help="work through the queue from the other end (lowest priority first): a second "
                         "machine meets the nightly pass in the middle instead of repeating its videos")
    ap.add_argument("--results-out", default=os.environ.get("OCR_RESULTS_OUT", ""),
                    help="write results to this small JSON (url -> OCR fields) instead of the cache; "
                         "merge_ocr_results.py applies it to the cache on the machine that owns it")
    a = ap.parse_args()
    _cookies_from_env()

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = data.get("reviews", [])
    if a.results_out:
        print(f"{apply_results(rows, load_results(a.results_out))} rows already done in {a.results_out}")
    if a.new:
        # A held video whose last download failed generically ("fail") is tried again; the
        # status is dropped in memory and the row is saved with it gone when it is processed.
        for r in rows:
            if r.get("ife_gate") == "pending":
                for k in ("chapters_ocr_status", "ife_tags_status"):
                    if r.get(k) == "fail":
                        del r[k]
        cutoff =(datetime.now(timezone.utc) - timedelta(days=a.since_days)).strftime("%Y-%m-%d")
        todo = [r for r in rows if r.get("media_type") == "video" and _yt_id(r.get("url"))
                and ((r.get("published_at") or "")[:10] >= cutoff or r.get("ife_gate") == "pending")
                and (_never_tried(r) or _tag_only(r))]
        # Best IFE-review candidates first, newest within a tier: when a night's budget
        # runs out, what waits is the least likely to be an IFE review.
        todo.sort(key=lambda r: (_long_priority(r), r.get("published_at") or ""), reverse=True)
    elif a.tag_chaptered:
        todo = [r for r in rows if r.get("media_type") == "video" and _yt_id(r.get("url")) and _tag_only(r)]
        todo.sort(key=lambda r: (_long_priority(r), r.get("published_at") or ""), reverse=True)
    elif a.url:
        # An explicit URL may be any video (OCR works on full-length reviews
        # too — it is just slower), not only a Short.
        todo = [r for r in rows if r.get("url") == a.url and _yt_id(r.get("url"))]
    elif a.ife_chapters:
        # Every incoming review with an IFE chapter gets that chapter tagged;
        # a video that already failed is not retried every night.
        todo = [r for r in rows if r.get("media_type") == "video" and _yt_id(r.get("url"))
                and r.get("chapters_source") in ("description", "transcript") and ife_spans(r)
                and _needs_ife_ocr(r)]
        todo.sort(key=lambda r: r.get("published_at") or "", reverse=not a.oldest_first)
        todo.sort(key=lambda r: bool(r.get("ife_tags_status")))     # never-tried first, then stale ones
    elif a.long:
        # Full-length external reviews without description chapters: OCR gives
        # them tag-only chapters (long_form merge rules). Newest first, and a
        # video that already failed is not retried every night.
        todo = [r for r in rows if r.get("media_type") == "video" and not _is_short(r) and _yt_id(r.get("url"))
                and not r.get("chapters_ocr_status")
                and (not r.get("chapters") or (r.get("chapters_source") == "ocr" and _stale(r)))]
        # Never-tried first, then ones an older tagger version read. Best IFE-review
        # candidates first within each (the backlog is thousands of videos and the
        # nightly budget covers a few dozen), newest within a tier.
        todo.sort(key=lambda r: (not r.get("chapters"), _long_priority(r), r.get("published_at") or ""), reverse=True)
    else:
        todo = [r for r in rows if _is_short(r) and _yt_id(r.get("url"))]
        # description chapters (YouTube's own) are authoritative — never replace them
        # A Short already OCR'd (possibly to zero chapters) is not redone unless --retag.
        # ...except that a Short an older tagger version read is redone, after the never-tried ones.
        todo = [r for r in todo if (not r.get("chapters") and r.get("chapters_source") != "ocr")
                or (r.get("chapters_source") == "ocr" and (a.retag or (_stale(r) and not r.get("chapters_ocr_status"))))]
        todo.sort(key=lambda r: r.get("chapters_source") == "ocr")
    if a.reverse:
        todo.reverse()
    if a.shard:
        k, n_sh = (int(x) for x in a.shard.split("/"))
        todo = todo[k::n_sh]
    if a.limit:
        todo = todo[:a.limit]
    kind = ('new videos' if a.new else 'chaptered videos without tags' if a.tag_chaptered
            else 'IFE chapters' if a.ife_chapters else 'full-length videos' if a.long else 'Shorts')
    print(f"OCR repo: {REPO}\n{len(todo)} {kind} to tag")
    if a.dry_run or not todo:
        return

    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()

    t0 = time.time()
    done = failed = 0
    stats = {"ok": 0, "skip": 0, "fail": 0, "block": 0}

    touched = {}

    def save():
        if a.results_out:
            save_results(a.results_out, touched)
            return
        # Merge, don't overwrite: the transcript backfill / media fetch / a
        # parallel shard may have written the cache since we loaded it. Re-read
        # under a lock and apply only our fields.
        with _cache_lock():
            fresh = json.loads(CACHE.read_text(encoding="utf-8"))
            for row in fresh.get("reviews", []):
                src = touched.get(row.get("url"))
                if src is not None:
                    for k in OWN:
                        if k in src:
                            row[k] = src[k]
                        else:
                            row.pop(k, None)
            CACHE.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")

    for n, r in enumerate(todo, 1):
        if a.max_runtime_min and (time.time() - t0) / 60 > a.max_runtime_min:
            print(f"Runtime cap reached after {n - 1} videos.")
            break
        vid = _yt_id(r["url"])
        print(f"\n[{n}/{len(todo)}] {vid}  {r.get('title', '')[:70]}")
        if a.tag_chaptered or (a.new and _tag_only(r)):
            with tempfile.TemporaryDirectory() as tmp:
                path, status = download(vid, tmp)
                stats[status if status in stats else "fail"] += 1
                if status == "block":
                    print("  YouTube is rate-limiting/bot-checking downloads — stopping this run.")
                    break
                chaps = None
                if path:
                    try:
                        chaps = ocr_chapters(path, ocr, a.interval, os.path.join(tmp, "out"), long_form=True)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  OCR failed: {exc}")
                        status = "ocr-error"
            if chaps is None:
                print(f"  {status}")
                r["ife_tags_status"] = status if status != "ok" else "fail"
                touched[r["url"]] = r
                failed += 1
                continue
            # The description chapters stay as they are; only the screen tags are added.
            tags = sorted({t for c in chaps for t in c["tags"]})
            r["screen_tags"] = tags
            r["screen_tag_source"] = "ocr-video"
            r["ife_tags_status"] = "done" if tags else "none"
            r["ocr_tagger_v"] = TAGGER_V
            r["ife_features"] = to_ife_features(tags, r.get("ife_features") or {})
            touched[r["url"]] = r
            done += 1
            print(f"  tags: {', '.join(tags) or '(no readable IFE screens)'}")
            if done % 10 == 0:
                save()
            continue
        if a.ife_chapters or (a.new and r.get("chapters_source") in ("description", "transcript")):
            dets, status = [], "ok"
            for ci, start, end in ife_spans(r):
                with tempfile.TemporaryDirectory() as tmp:
                    path, status = download(vid, tmp, ranges=[(start, end)])
                    if status == "block" or not path:
                        break
                    try:
                        rows_ = ocr_chapters(path, ocr, a.interval, os.path.join(tmp, "out"), long_form=True)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  OCR failed: {exc}")
                        status = "ocr-error"
                        break
                dets += tag_detections(rows_, start, ci, a.interval)
            stats[status if status in stats else "fail"] += 1
            if status == "block":
                print("  YouTube is rate-limiting/bot-checking downloads — stopping this run.")
                break
            if status != "ok":
                print(f"  {status}")
                r["ife_tags_status"] = status
                touched[r["url"]] = r
                failed += 1
                continue
            r["ife_tags"] = dets
            r["ife_tags_status"] = "done" if dets else "none"
            r["ocr_tagger_v"] = TAGGER_V
            tags = _ocr_screen_tags(r)
            if tags or r.get("screen_tags"):
                r["screen_tags"] = tags          # rebuilt: an older tagger's mistakes do not survive
                if tags:
                    r.setdefault("screen_tag_source", "ocr-ife-chapter")
                r["ife_features"] = to_ife_features(tags, r.get("ife_features") or {})
            touched[r["url"]] = r
            done += 1
            for d in dets:
                print(f"  {d['t']:>6}  {d['tag']}  ({int(d['conf'] * 100)}%)")
            if not dets:
                print("  (no readable IFE screens in the IFE chapter)")
            if done % 10 == 0:
                save()
            continue
        with tempfile.TemporaryDirectory() as tmp:
            path, status = download(vid, tmp)
            stats[status] += 1
            if status == "block":
                print("  YouTube is rate-limiting/bot-checking downloads — stopping this run.")
                break
            if not path:
                print(f"  download {status}")
                r["chapters_ocr_status"] = status
                touched[r["url"]] = r
                failed += 1
                continue
            try:
                chaps = ocr_chapters(path, ocr, a.interval, os.path.join(tmp, "out"), long_form=not _is_short(r))
            except Exception as exc:  # noqa: BLE001
                print(f"  OCR failed: {exc}")
                r["chapters_ocr_status"] = "ocr-error"
                touched[r["url"]] = r
                failed += 1
                continue
        r["chapters"] = chaps
        r["chapters_source"] = "ocr"
        r["ocr_tagger_v"] = TAGGER_V
        r.pop("chapters_ocr_status", None)
        tags = sorted({t for c in chaps for t in c["tags"]})
        r["screen_tags"] = tags
        r["screen_tag_source"] = "ocr-video"
        r["ife_features"] = to_ife_features(tags, r.get("ife_features") or {})
        touched[r["url"]] = r
        done += 1
        for c in chaps:
            print(f"  {c['t']:>5}  {'✦ ' if c['ife'] else ''}{c['title']}")
        if not chaps:
            print("  (no readable IFE screens)")
        if done % 10 == 0:
            save()
    save()
    print(f"\nDone: {done} chaptered, {failed} failed  {stats}  in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
