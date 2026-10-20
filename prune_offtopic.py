"""
Re-apply the crawler's off-topic / news / spam gate to cached YouTube entries
and drop the ones that would not be accepted today.

Only entries with no IFE evidence are candidates: no detected system, no OCR
screen tags, no IFE feature beyond "seat", no transcript. Videos the OCR pass
has already sampled without finding any IFE screen are dropped when nothing
else (system, text features) says IFE either. Trusted reviewer
channels are never pruned. Everything removed is written to
pruned_offtopic.json so it can be inspected or restored.

Run:  python prune_offtopic.py            (dry run: prints what would go)
      python prune_offtopic.py --apply
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("IFE_NO_TRANSLATE", "1")   # gate on the raw title; no network

import pruned_urls  # noqa: E402
from ife_crawler import (IFECrawler, _is_offtopic_video, _is_trusted_channel, _gate_title,  # noqa: E402
                         AIRLINE_KEYWORDS, GLOBAL_AIRLINES)

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


def should_prune(r):
    if "youtube.com" not in (r.get("url") or ""):
        return False
    ch = r.get("channel_title") or ""
    title = _gate_title(r.get("title") or "", r.get("title_en"))
    if _CRAWLER._has_ife_keyword(title, skip_broad=True):
        return False      # explicit IFE subject: a news outlet's IFE segment still counts
    if is_airline_channel(ch):
        # An airline's own upload: features inferred from its boilerplate
        # description ("enjoy our award-winning entertainment...") are not
        # evidence. Only the title, OCR tags or a detected system keep it.
        return not (r.get("ife_system") or r.get("screen_tags"))
    if ocr_found_nothing(r):
        return True       # the OCR pass watched it and saw no IFE screen, and nothing else says IFE
    if has_ife_evidence(r):
        return False
    if _is_trusted_channel(ch):
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
    urls = gone_urls | {r["url"] for r in resurrected}
    data["reviews"] = [r for r in rows if r["url"] not in urls]
    old = json.loads(PRUNED.read_text(encoding="utf-8")) if PRUNED.exists() else []
    PRUNED.write_text(json.dumps(old + [r for r in gone if r["url"] not in listed], indent=1, ensure_ascii=False),
                      encoding="utf-8")
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"removed {len(urls)}; {len(data['reviews'])} remain; copies in {PRUNED.name}")


if __name__ == "__main__":
    main()
