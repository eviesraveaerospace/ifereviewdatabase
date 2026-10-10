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


def download(video_id: str, workdir: str):
    """→ (path | None, status) where status ∈ ok / skip / block / fail."""
    import yt_dlp
    logger = _SilentLogger()
    opts = {
        "format": "bv*[height<=480][ext=mp4]/b[height<=480]/b",
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "quiet": True, "no_warnings": True, "nocheckcertificate": True, "logger": logger,
        "extractor_args": {"youtube": {"player_client": ["tv", "android", "web_safari"]}},
    }
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
        if rows and rows[-1]["tags"] == tags and (tags or rows[-1]["title"] == title)                 and ch["start"] - rows[-1]["end"] <= (gap if long_form else 0.01):
            rows[-1]["end"] = ch["end"]
            continue
        rows.append({"t": fmt_ts(ch["start"]), "sec": int(round(ch["start"])), "end": int(round(ch["end"])),
                     "title": title[:90], "tags": tags, "ife": bool(tags), "source": "ocr"})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N videos (0 = all)")
    ap.add_argument("--url", help="only this video URL")
    ap.add_argument("--retag", action="store_true", help="redo Shorts that already have OCR chapters")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between sampled frames")
    ap.add_argument("--max-runtime-min", type=float, default=float(os.environ.get("MAX_RUNTIME_MIN", 0) or 0))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--long", action="store_true",
                    help="full-length videos (not Shorts) that have no chapters at all, newest first")
    a = ap.parse_args()
    _cookies_from_env()

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = data.get("reviews", [])
    if a.url:
        # An explicit URL may be any video (OCR works on full-length reviews
        # too — it is just slower), not only a Short.
        todo = [r for r in rows if r.get("url") == a.url and _yt_id(r.get("url"))]
    elif a.long:
        # Full-length external reviews without description chapters: OCR gives
        # them tag-only chapters (long_form merge rules). Newest first, and a
        # video that already failed is not retried every night.
        todo = [r for r in rows if r.get("media_type") == "video" and not _is_short(r) and _yt_id(r.get("url"))
                and not r.get("chapters") and not r.get("chapters_ocr_status")]
        # Best IFE-review candidates first (the backlog is thousands of videos
        # and the nightly budget covers a few dozen), newest within a tier.
        todo.sort(key=lambda r: (_long_priority(r), r.get("published_at") or ""), reverse=True)
    else:
        todo = [r for r in rows if _is_short(r) and _yt_id(r.get("url"))]
        # description chapters (YouTube's own) are authoritative — never replace them
        todo = [r for r in todo if not r.get("chapters") or (a.retag and r.get("chapters_source") == "ocr")]
    if a.limit:
        todo = todo[:a.limit]
    print(f"OCR repo: {REPO}\n{len(todo)} {'full-length videos' if a.long else 'Shorts'} to chapter")
    if a.dry_run or not todo:
        return

    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()

    t0 = time.time()
    done = failed = 0
    stats = {"ok": 0, "skip": 0, "fail": 0, "block": 0}

    OWN = ("chapters", "chapters_source", "chapters_ocr_status", "screen_tags", "screen_tag_source", "ife_features")
    touched = {}

    def save():
        # Merge, don't overwrite: the transcript backfill / media fetch may have
        # written the cache since we loaded it. Re-read and apply only our fields.
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
