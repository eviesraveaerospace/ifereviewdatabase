"""
Re-apply the crawler's off-topic / news / spam gate to cached YouTube entries
and drop the ones that would not be accepted today.

Only entries with no IFE evidence are candidates: no detected system, no OCR
screen tags, no IFE feature beyond "seat", no transcript. Videos the OCR pass
has already sampled without finding any IFE screen are dropped when nothing
else (system, text features) says IFE either. A video with a usable transcript that
never talks about the inflight entertainment is dropped too. A new video is held
(ife_gate "pending", hidden from the dashboard) until it has chapters and tags; one the
OCR pass has read that still lacks them is dropped, trusted channels included. Trusted
reviewer channels are otherwise never pruned. Everything removed is written to
pruned_offtopic.json so it can be inspected or restored.

Run:  python prune_offtopic.py            (dry run: prints what would go)
      python prune_offtopic.py --apply
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("IFE_NO_TRANSLATE", "1")   # gate on the raw title; no network

import pruned_urls  # noqa: E402
from ife_crawler import (IFECrawler, _is_offtopic_video, _is_trusted_channel, _gate_title,  # noqa: E402
                         AIRLINE_KEYWORDS, GLOBAL_AIRLINES, junk_transcript, transcript_ife_evidence,
                         transcript_words, TRANSCRIPT_MIN_WORDS)

_AIRLINE_NAMES = set(AIRLINE_KEYWORDS) | {a.lower() for a in GLOBAL_AIRLINES}


def is_airline_channel(name):
    n = (name or "").strip().lower().replace(" (official)", "")
    return bool(n) and n in _AIRLINE_NAMES

_CRAWLER = IFECrawler(verify_ssl=False)

CACHE = Path(__file__).parent / "ife_cache.json"
PRUNED = Path(__file__).parent / "pruned_offtopic.json"


def has_ife_evidence(r):
    feats = {k for k in (r.get("ife_features") or {}) if k != "seat"}
    return bool(r.get("ife_system") or r.get("screen_tags") or feats or r.get("transcript_available"))


def ocr_found_nothing(r):
    """OCR has sampled this video and tagged no IFE screen, and neither the
    text nor a detected system claims one. A transcript alone is not enough:
    captions exist for almost every video."""
    processed = r.get("chapters_source") == "ocr" or r.get("screen_tag_source") == "ocr-video"
    feats = {k for k in (r.get("ife_features") or {}) if k != "seat"}
    return processed and not r.get("screen_tags") and not r.get("ife_system") and not feats


GATE_WAIT_DAYS = 30    # a held video OCR never reached (or that nothing would read) is dropped after this long


def _speaks_of_ife(r):
    return any(transcript_ife_evidence(r.get(k) or "") for k in ("transcript_full", "transcript_full_en"))


def _usable_transcript(r):
    """A transcript long enough to judge the video on: shorter ones are music over
    the footage and say nothing either way (OCR is the evidence for those)."""
    return (bool(r.get("transcript_available")) and not junk_transcript(r)
            and transcript_words(r) >= TRANSCRIPT_MIN_WORDS)


def has_chapters_and_tags(r):
    """What a new video must have before it is shown: chapters, and OCR/IFE tags."""
    return bool(r.get("chapters")) and bool(r.get("screen_tags") or r.get("ife_tags"))


def ocr_tried(r):
    """An OCR pass has read this video (whatever it found). A generic download
    failure ("fail") may be transient, so it does not count: that video waits out
    GATE_WAIT_DAYS instead."""
    return bool((r.get("chapters_ocr_status") and r.get("chapters_ocr_status") != "fail") or r.get("ife_tags_status")
                or r.get("chapters_source") == "ocr" or r.get("screen_tag_source") == "ocr-video")


def gate_verdict(r, weak=False):
    """Fate of a video held at admission (ife_gate == "pending"): "keep" once it has
    chapters and tags, "drop" when OCR has read it and it still lacks them, when it
    was admitted on weak evidence (no IFE word in title or description, not a trusted
    channel) and its transcript never talks about the IFE, or when it has waited
    GATE_WAIT_DAYS; otherwise "wait" for the nightly OCR pass."""
    if has_chapters_and_tags(r):
        return "keep"
    if ocr_tried(r):
        return "drop"
    if weak and _usable_transcript(r) and not _speaks_of_ife(r):
        return "drop"
    try:
        age = (datetime.now(timezone.utc).date() - date.fromisoformat(r.get("ife_gate_since") or "")).days
    except ValueError:
        age = GATE_WAIT_DAYS
    return "drop" if age >= GATE_WAIT_DAYS else "wait"


def should_prune(r):
    if "youtube.com" not in (r.get("url") or ""):
        return False
    ch = r.get("channel_title") or ""
    title = _gate_title(r.get("title") or "", r.get("title_en"))
    ife_title = _CRAWLER._has_ife_keyword(title, skip_broad=True)
    if r.get("ife_gate") == "pending":
        return gate_verdict(r, weak=not ife_title and not _is_trusted_channel(ch)) == "drop"
    if ife_title:
        return False      # explicit IFE subject: a news outlet's IFE segment still counts
    if is_airline_channel(ch):
        # An airline's own upload: features inferred from its boilerplate
        # description ("enjoy our award-winning entertainment...") are not
        # evidence. Only the title, OCR tags or a detected system keep it.
        return not (r.get("ife_system") or r.get("screen_tags"))
    if ocr_found_nothing(r):
        return True       # the OCR pass watched it and saw no IFE screen, and nothing else says IFE
    if _is_trusted_channel(ch):
        return False
    if _usable_transcript(r) and not (r.get("ife_system") or r.get("screen_tags")) and not _speaks_of_ife(r):
        return True       # the transcript never talks about the IFE, and nothing else says it does
    if has_ife_evidence(r):
        return False
    # Off-topic only (news, hobby/sim, story narration, documentaries, other
    # transport). The duration-based Shorts rule in _is_spam_video is left out:
    # it would also drop short cabin reviews that were accepted on purpose.
    return _is_offtopic_video(title, ch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = data.get("reviews", [])
    gone = [r for r in rows if should_prune(r)]
    cleared = [r for r in rows if r.get("ife_gate") == "pending" and r not in gone and gate_verdict(r) == "keep"]
    waiting = sum(1 for r in rows if r.get("ife_gate") == "pending" and r not in gone and r not in cleared)
    print(f"{len(cleared)} held videos now have chapters + tags; {waiting} still wait for OCR")
    # Rows already in the pruned list that a cache merge brought back: drop them
    # again (the list already holds their copy).
    listed = pruned_urls.load()
    gone_urls = {r["url"] for r in gone}
    resurrected = [r for r in rows if r.get("url") in listed and r["url"] not in gone_urls
                   and r.get("media_type") != "internal"]
    print(f"{len(gone)} of {len(rows)} entries would be pruned; {len(resurrected)} more are back from the pruned list")
    for r in gone[:60]:
        print(f"  {r.get('year')}  {(r.get('channel_title') or '')[:24]:24s}  {(r.get('title') or '')[:70]}")
    if len(gone) > 60:
        print(f"  ... and {len(gone) - 60} more")
    if not a.apply:
        return
    for r in cleared:
        r.pop("ife_gate", None)
        r.pop("ife_gate_since", None)
    urls = gone_urls | {r["url"] for r in resurrected}
    data["reviews"] = [r for r in rows if r["url"] not in urls]
    old = json.loads(PRUNED.read_text(encoding="utf-8")) if PRUNED.exists() else []
    PRUNED.write_text(json.dumps(old + [r for r in gone if r["url"] not in listed], indent=1, ensure_ascii=False),
                      encoding="utf-8")
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"removed {len(urls)}; {len(data['reviews'])} remain; copies in {PRUNED.name}")


if __name__ == "__main__":
    main()
