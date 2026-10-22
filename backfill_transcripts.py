"""
Retroactively fetches transcripts for all YouTube videos in the cache
that currently have no transcript.

Strategy (in order):
  1. youtube-transcript-api  — fast, free, manual + auto captions
  2. Local Whisper via yt-dlp — covers videos with no YouTube captions at all

Env vars:
  YOUTUBE_COOKIES_B64  — base64-encoded Netscape cookies.txt (bypasses IP block)
  WHISPER_MODEL        — Whisper model size (default: base)
"""
import base64
import json
import re
import os
import sys
import time
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Pick up YOUTUBE_COOKIES_B64 / WHISPER_MODEL from a local .env (CI sets them
# directly in the job environment, which load_dotenv leaves untouched).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from youtube_transcript_api import YouTubeTranscriptApi

# Write cookies.txt from env if provided, return path or None
def _setup_cookies(tmpdir: str) -> str | None:
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        path = os.path.join(tmpdir, "cookies.txt")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path
    except Exception as e:
        print(f"  Warning: could not decode YOUTUBE_COOKIES_B64 — {e}")
        return None

from ife_crawler import IFECrawler, IFE_FEATURE_KEYWORDS, IFE_SYSTEM_PATTERNS

# Reuse the crawler's strict IFE caption picker (drops greetings/filler, no
# evenly-spaced padding) so Whisper captions match the rest of the app.
_SCORE_KWS = (
    [kw for kws in IFE_FEATURE_KEYWORDS.values() for kw in kws]
    + [p for ps in IFE_SYSTEM_PATTERNS.values() for p in ps]
)


def segs_to_result(segs):
    excerpt, caps, full = IFECrawler._segs_to_caps(segs, _SCORE_KWS)
    return caps, excerpt, full


# Helper-only crawler instance (no network setup) for text analysis.
_analyzer = IFECrawler.__new__(IFECrawler)
from ife_crawler import _extract_specs, AIRLINE_KEYWORDS, AIRCRAFT_KEYWORDS, infer_ife_system

# Fields (re)derived from the transcript — merged into the cache alongside the
# transcript itself.
ENRICH_KEYS = ("ife_system", "ife_system_guess", "ife_features", "ife_specs",
               "airlines_mentioned", "aircraft_mentioned")


def enrich_from_transcript(r):
    """Re-run system/feature/spec/mention detection now that the transcript
    exists — crawl-time detection only saw title+description, so systems that
    are named only in speech (very common) were never tagged."""
    text = (r.get("title", "") + " " + (r.get("transcript_full") or "")).lower()
    detected = _analyzer._detect_system(text)
    if detected and not r.get("ife_system"):
        r["ife_system"] = detected
        r["ife_system_inferred"] = False
    feats = dict(r.get("ife_features") or {})
    feats.update(_analyzer._features(text))
    r["ife_features"] = feats
    specs = _extract_specs(text)
    specs.update(r.get("ife_specs") or {})   # keep existing values on conflict
    r["ife_specs"] = specs
    r["airlines_mentioned"] = _analyzer._mentions(text, AIRLINE_KEYWORDS)
    r["aircraft_mentioned"] = _analyzer._mentions(text, AIRCRAFT_KEYWORDS)
    if not r.get("ife_system"):
        r["ife_system_guess"] = infer_ife_system(r["airlines_mentioned"], r["aircraft_mentioned"])
    # A transcript that names the entertainment system gives the video an IFE
    # chapter when the description had none (ife_crawler.transcript_ife_chapter).
    from ife_crawler import add_transcript_ife_chapter
    add_transcript_ife_chapter(r)


LAST_FETCH_BLOCKED = False   # set when YouTube refused the request rather than lacking captions
LAST_FETCH_DEFINITIVE = False  # set when YouTube said outright there are no captions
_NO_CAPTION_ERRORS = ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "VideoUnplayable")


def fetch_yt_segs(video_id, cookies_path=None):
    """Try YouTube transcript API (manual → auto-generated → any language)."""
    global LAST_FETCH_BLOCKED, LAST_FETCH_DEFINITIVE
    LAST_FETCH_BLOCKED = LAST_FETCH_DEFINITIVE = False
    try:
        api = YouTubeTranscriptApi(cookies=cookies_path) if cookies_path else YouTubeTranscriptApi()
    except TypeError:
        api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=[
            "en", "en-US", "en-GB",
            "fr", "de", "ja", "ko", "zh", "zh-TW", "zh-CN",
            "es", "pt", "tr", "it", "ar", "nl", "fi", "no",
        ])
        return [{"text": s.text, "start": s.start} for s in fetched]
    except Exception:
        pass
    try:
        tlist = api.list(video_id)
        for t in sorted(tlist, key=lambda x: x.is_generated):
            try:
                segs = [{"text": s.text, "start": s.start} for s in t.fetch()]
                if segs:
                    return segs
            except Exception:
                continue
    except Exception as exc:
        name = type(exc).__name__
        LAST_FETCH_BLOCKED = "Blocked" in name or "TooMany" in name or "429" in str(exc)
        LAST_FETCH_DEFINITIVE = name in _NO_CAPTION_ERRORS
    return None


def load_whisper_model():
    """Load local Whisper model once. Returns None if not installed."""
    try:
        import whisper
        size = os.environ.get("WHISPER_MODEL", "base")
        print(f"Loading Whisper '{size}' model (first run downloads ~145MB)...")
        return whisper.load_model(size)
    except ImportError:
        print("openai-whisper not installed — run: pip install openai-whisper")
        return None
    except Exception as e:
        print(f"Whisper load error: {e}")
        return None


# Broadened from a single incident — yt-dlp/YouTube phrase bot-detection and
# rate-limiting differently depending on which wall you hit.
_BOT_SIGNALS = (
    "sign in to confirm", "not a bot", "sign in if you", "unusual traffic",
    "confirm you're not a bot", "confirm that you are not a bot",
    "429", "too many requests", "rate limit", "http error 403", "forbidden",
)
_SKIP_SIGNALS = ("private video", "video unavailable", "has been removed", "account has been terminated")

# Return values for fetch_whisper_segs
_WHISPER_OK    = "ok"
_WHISPER_SKIP  = "skip"   # private/unavailable — remove from cache
_WHISPER_BLOCK = "block"  # bot-detected — stop trying Whisper for this run
_WHISPER_FAIL  = "fail"   # generic failure


class _SilentLogger:
    """Captures yt-dlp error text without printing it."""
    def __init__(self):
        self.errors = []
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): self.errors.append(msg.lower())


def _browser_cookie_opts():
    """Try to borrow a real logged-in YouTube session from a local browser —
    far less likely to look like a bot than an anonymous request. Silently
    unavailable on CI runners / machines with no matching browser profile."""
    for browser in ("edge", "chrome", "firefox"):
        try:
            import yt_dlp
            # Cheap probe: let yt-dlp's own cookie extractor try to open the
            # profile. If it can't find/decrypt one, this raises.
            from yt_dlp.cookies import extract_cookies_from_browser
            extract_cookies_from_browser(browser)
            return {"cookiesfrombrowser": (browser,)}
        except Exception:
            continue
    return {}


def fetch_whisper_segs(model, video_id, cookies_path=None, browser_cookie_opts=None):
    """Download audio with yt-dlp and transcribe with local Whisper.
    Returns (status, segs, detail) where status is one of the _WHISPER_*
    constants and detail is a short human-readable reason (for FAIL/BLOCK)."""
    if model is None:
        return _WHISPER_FAIL, None, "whisper model not loaded"
    try:
        import yt_dlp
    except ImportError:
        return _WHISPER_FAIL, None, "yt-dlp not installed"

    logger = _SilentLogger()
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                "format": "worstaudio/worst/bestaudio/best",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "nocheckcertificate": True,
                "logger": logger,
                # YouTube hides formats from the default web client behind PO
                # tokens ("Requested format is not available"); the tv/android
                # clients still serve plain formats.
                "extractor_args": {"youtube": {"player_client": ["tv", "android", "web_safari"]}},
            }
            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path
            elif browser_cookie_opts:
                ydl_opts.update(browser_cookie_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # Check captured errors even if download didn't raise
            combined = " ".join(logger.errors)
            if any(s in combined for s in _SKIP_SIGNALS):
                return _WHISPER_SKIP, None, combined[:200]
            if any(s in combined for s in _BOT_SIGNALS):
                return _WHISPER_BLOCK, None, combined[:200]

            files = os.listdir(tmpdir)
            if not files:
                return _WHISPER_FAIL, None, ("yt-dlp produced no file; " + combined[:150]) if combined else "yt-dlp produced no output file"
            audio_path = os.path.join(tmpdir, files[0])
            file_size = os.path.getsize(audio_path)
            result = model.transcribe(audio_path, verbose=False)

        segments = result.get("segments") or []
        if not segments:
            # A suspiciously tiny download (a few KB) alongside zero speech is a
            # much more likely sign of a blocked/placeholder stream than of a
            # genuinely silent multi-minute review video.
            if file_size < 30_000:
                return _WHISPER_BLOCK, None, f"no speech detected + tiny audio file ({file_size}B) — likely blocked/placeholder stream"
            return _WHISPER_FAIL, None, "no speech detected in audio"
        return _WHISPER_OK, [{"text": seg["text"], "start": seg["start"]} for seg in segments], None

    except Exception as e:
        msg = str(e).lower()
        if any(s in msg for s in _SKIP_SIGNALS):
            return _WHISPER_SKIP, None, str(e)[:200]
        if any(s in msg for s in _BOT_SIGNALS):
            return _WHISPER_BLOCK, None, str(e)[:200]
        return _WHISPER_FAIL, None, str(e)[:200]


def _junk_transcript(r):
    """A YouTube transcript that says nothing about the review: mostly music tags
    ("Heat.") or under 20 words a minute on a video of 5 min or more. Whisper
    should redo it instead of the video counting as transcribed forever; a video
    Whisper already failed on is left alone."""
    if r.get("transcript_source") == "whisper" or r.get("whisper_status") == "fail":
        return False
    mins = (r.get("duration_seconds") or 0) / 60
    if mins < 5:
        return False
    words = re.findall(r"[A-Za-z']+", r.get("transcript_full") or "")
    heat = sum(w.lower() == "heat" for w in words)
    return len(words) / mins < 20 or (len(words) >= 20 and heat / len(words) > 0.2)


def main():
    with open("ife_cache.json", encoding="utf-8") as f:
        data = json.load(f)

    reviews = data.get("reviews", [])
    targets = [
        r for r in reviews
        if ("youtube.com/watch?v=" in r.get("url", "") or "/shorts/" in r.get("url", ""))
        and (not r.get("transcript_available") or _junk_transcript(r))
    ]
    # Shorts are under 3 minutes, so a whole night of them transcribes hundreds of
    # reels; do them before the long-form backlog.
    # Then the videos most likely to be IFE reviews (a description chapter flagged
    # IFE, a named system, several IFE features), newest first — not cache order,
    # which left recent reviews behind a years-old backlog.
    def _prio(r):
        feats = {k for k in (r.get("ife_features") or {}) if k != "seat"}
        return (bool(r.get("is_short") or "/shorts/" in r.get("url", "")),
                any(c.get("ife") for c in r.get("chapters") or []),
                bool(r.get("ife_system")), min(len(feats), 3), r.get("published_at") or "")
    targets.sort(key=_prio, reverse=True)
    # CAPTIONS_ONLY=1 (the laptop, whose egress YouTube serves captions to):
    # YouTube captions only, no Whisper; a video with none is marked so the
    # VM's Whisper night takes those first and never re-tries ones with captions.
    captions_only = os.environ.get("CAPTIONS_ONLY", "").strip() not in ("", "0")
    if captions_only:
        targets = [r for r in targets if r.get("yt_captions") != "none" and not _junk_transcript(r)]
        print(f"Captions-only mode: {len(targets)} videos not yet checked")
    else:
        # Whisper is slow: spend it where captions are known to be absent first.
        targets.sort(key=lambda r: not (r.get("yt_captions") == "none" or _junk_transcript(r)))
    print(f"Videos without transcript: {len(targets)}")

    # Set up cookies (bypasses IP block on GitHub Actions datacenter IPs)
    _cookie_dir = tempfile.mkdtemp()
    cookies_path = _setup_cookies(_cookie_dir)
    browser_cookie_opts = {}
    if cookies_path:
        print("  Using YOUTUBE_COOKIES_B64 for authentication.")
    else:
        browser_cookie_opts = _browser_cookie_opts()
        if browser_cookie_opts:
            print(f"  Using local browser cookies ({browser_cookie_opts['cookiesfrombrowser'][0]}) for authentication.")
        else:
            print("  No cookies available (no YOUTUBE_COOKIES_B64, no usable local browser profile) — some videos may be IP-blocked.")

    whisper_model = None if captions_only else load_whisper_model()
    yt_ok = whisper_ok = skipped = blocked = failed = 0
    to_remove = []   # private / unavailable video URLs
    processed = set()  # urls whose transcript fields we set this run
    whisper_blocked = False

    def merge_save():
        """Save by merging OUR transcript updates into the CURRENT cache file,
        so reviews/tags added by the dashboard while we grind aren't clobbered
        by a whole-file dump of our stale in-memory copy."""
        try:
            with open("ife_cache.json", encoding="utf-8") as f:
                fresh = json.load(f)
        except (ValueError, OSError):
            fresh = data
        if fresh is not data:
            by_url = {fr.get("url"): fr for fr in fresh.get("reviews", [])}
            for rr in data.get("reviews", []):
                u = rr.get("url")
                if u in processed and u in by_url:
                    tgt = by_url[u]
                    for k in ("transcript_available", "transcript_excerpt", "captions",
                              "transcript_full", "transcript_source", "chapters", "chapters_source",
                              "yt_captions", "whisper_status") + ENRICH_KEYS:
                        if k in rr:
                            tgt[k] = rr[k]
            if to_remove:
                dead = set(to_remove)
                fresh["reviews"] = [fr for fr in fresh["reviews"] if fr.get("url") not in dead]
        with open("ife_cache.json", "w", encoding="utf-8") as f:
            json.dump(fresh, f, indent=2, ensure_ascii=False)

    # Optional wall-clock budget (minutes). CI kills jobs at 6 h and the commit
    # step only runs after this script exits — stop early so progress is saved.
    budget_min = float(os.environ.get("MAX_RUNTIME_MIN", 0) or 0)
    started = time.time()

    for idx, r in enumerate(targets, 1):
        if budget_min and (time.time() - started) > budget_min * 60:
            print(f"\nTime budget of {budget_min:.0f} min reached at video {idx}/{len(targets)} — saving and exiting.")
            break
        vid_id = r["url"].split("watch?v=")[1].split("&")[0]

        # 1. YouTube transcript API
        segs = None if _junk_transcript(r) else fetch_yt_segs(vid_id, cookies_path=cookies_path)
        if segs:
            caps, excerpt, full = segs_to_result(segs)
            r["transcript_available"] = True
            r["transcript_excerpt"] = excerpt
            r["captions"] = caps
            r["transcript_full"] = full
            r["transcript_source"] = "youtube"
            r["yt_captions"] = "ok"
            enrich_from_transcript(r)
            processed.add(r["url"])
            yt_ok += 1
            print(f"[{idx}/{len(targets)}] YT-OK    {vid_id}  ({len(caps)} caps)")

        elif captions_only and LAST_FETCH_BLOCKED:
            print(f"[{idx}/{len(targets)}] BLOCKED  {vid_id}  — YouTube is refusing caption requests; stopping this run.")
            break

        elif captions_only and not LAST_FETCH_DEFINITIVE:
            # A track exists but the fetch failed (transient): leave it unmarked so
            # the next night retries instead of handing it to Whisper.
            failed += 1
            print(f"[{idx}/{len(targets)}] RETRY    {vid_id}  (caption fetch failed)")

        elif captions_only:
            r["yt_captions"] = "none"
            processed.add(r["url"])
            failed += 1
            print(f"[{idx}/{len(targets)}] NO-CAPS  {vid_id}")

        elif whisper_blocked:
            failed += 1
            print(f"[{idx}/{len(targets)}] SKIP-WH  {vid_id}  (bot-blocked on this runner)")

        else:
            # 2. Local Whisper fallback
            status, segs, detail = fetch_whisper_segs(
                whisper_model, vid_id, cookies_path=cookies_path, browser_cookie_opts=browser_cookie_opts
            )
            if status == _WHISPER_OK:
                caps, excerpt, full = segs_to_result(segs)
                r["transcript_available"] = True
                r["transcript_excerpt"] = excerpt
                r["captions"] = caps
                r["transcript_full"] = full
                r["transcript_source"] = "whisper"
                enrich_from_transcript(r)
                processed.add(r["url"])
                whisper_ok += 1
                print(f"[{idx}/{len(targets)}] WH-OK    {vid_id}  ({len(caps)} caps)")
            elif status == _WHISPER_SKIP:
                to_remove.append(r["url"])
                skipped += 1
                print(f"[{idx}/{len(targets)}] REMOVED  {vid_id}  (private/unavailable)")
            elif status == _WHISPER_BLOCK:
                whisper_blocked = True
                failed += 1
                print(f"[{idx}/{len(targets)}] BOT-BLOCK {vid_id}  ({detail or 'Whisper disabled for this run'})")
            else:
                failed += 1
                if r.get("transcript_available"):      # a junk YouTube transcript Whisper could not improve
                    r["whisper_status"] = "fail"
                    processed.add(r["url"])
                print(f"[{idx}/{len(targets)}] FAIL     {vid_id}  ({detail or 'unknown reason'})")

        if idx % 25 == 0:
            merge_save()
            print(f"  -- checkpoint: YT={yt_ok} Whisper={whisper_ok} removed={skipped} fail={failed} --")

        time.sleep(0.2 if captions_only else 0.5)

    if to_remove:
        print(f"\nPurging {len(set(to_remove))} private/unavailable videos.")
    merge_save()

    print(f"\nDone.")
    print(f"  YouTube captions:  {yt_ok}")
    print(f"  Whisper:           {whisper_ok}")
    print(f"  Purged (private):  {skipped}")
    print(f"  Still missing:     {failed}")
    if whisper_blocked:
        print("  NOTE: Whisper was bot-blocked — re-run locally with browser cookies for remaining videos.")


if __name__ == "__main__":
    main()
