"""
Re-apply the crawler's off-topic / news / spam gate to cached YouTube entries
and drop the ones that would not be accepted today.

Only entries with no IFE evidence are candidates: no detected system, no OCR
screen tags, no IFE feature beyond "seat", no transcript. Trusted reviewer
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

from ife_crawler import IFECrawler, _is_offtopic_video, _is_trusted_channel, _gate_title  # noqa: E402

_CRAWLER = IFECrawler(verify_ssl=False)

CACHE = Path(__file__).parent / "ife_cache.json"
PRUNED = Path(__file__).parent / "pruned_offtopic.json"


def has_ife_evidence(r):
    feats = {k for k in (r.get("ife_features") or {}) if k != "seat"}
    return bool(r.get("ife_system") or r.get("screen_tags") or feats or r.get("transcript_available"))


def should_prune(r):
    if "youtube.com" not in (r.get("url") or "") or has_ife_evidence(r):
        return False
    ch = r.get("channel_title") or ""
    if _is_trusted_channel(ch):
        return False
    title = _gate_title(r.get("title") or "", r.get("title_en"))
    if _CRAWLER._has_ife_keyword(title, skip_broad=True):
        return False      # explicit IFE subject: a news outlet's IFE segment still counts
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
    print(f"{len(gone)} of {len(rows)} entries would be pruned")
    for r in gone[:60]:
        print(f"  {r.get('year')}  {(r.get('channel_title') or '')[:24]:24s}  {(r.get('title') or '')[:70]}")
    if len(gone) > 60:
        print(f"  ... and {len(gone) - 60} more")
    if not a.apply:
        return
    urls = {r["url"] for r in gone}
    data["reviews"] = [r for r in rows if r["url"] not in urls]
    old = json.loads(PRUNED.read_text(encoding="utf-8")) if PRUNED.exists() else []
    PRUNED.write_text(json.dumps(old + gone, indent=1, ensure_ascii=False), encoding="utf-8")
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"removed {len(gone)}; {len(data['reviews'])} remain; copies in {PRUNED.name}")


if __name__ == "__main__":
    main()
