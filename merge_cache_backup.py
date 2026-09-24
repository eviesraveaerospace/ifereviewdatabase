"""Fold reviews from a backup cache into ife_cache.json without losing anything.

Usage:  python merge_cache_backup.py <backup.json> [--dry-run]

Used after a `git pull` had to discard local cache edits (e.g. the VM's own
background crawl wrote rows that conflicted with upstream). For every review in
the backup:
  * URL not in the current cache -> appended, unless it fails the crawler's
    relevance gate (so purged junk does not come back).
  * URL already present -> richer transcript/captions/chapters from the backup
    fill in gaps; every other field already in the cache is left alone.
The dashboard re-reads the cache from disk on every request, so no restart.
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import ife_crawler as _ic
from ife_data_manager import IFEDataManager

sys.stdout.reconfigure(encoding="utf-8")

CACHE = Path(__file__).parent / "ife_cache.json"
_gate = _ic.IFECrawler.__new__(_ic.IFECrawler)   # helpers only, no network setup
_TRUSTED = {n.lower().replace(" (official)", "") for n in _ic.KNOWN_IFE_CHANNELS}


def _passes_gate(r):
    if r.get("media_type") != "video":
        return True
    title = r.get("title", "")
    channel = (r.get("channel_title") or "").strip().lower()
    return (
        channel in _TRUSTED
        or _gate._has_ife_keyword(title)
        or _gate._is_aviation_review(title)
        or bool(r.get("ife_system"))
    )


def _richness(r):
    return (bool(r.get("transcript_available")), len(r.get("captions") or []),
            len(r.get("chapters") or []))


MERGE_FIELDS = ("transcript_available", "transcript_full", "transcript_full_en",
                "transcript_excerpt", "captions", "chapters", "comments",
                "article_text", "article_quotes")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        sys.exit(__doc__)
    backup = json.load(open(args[0], encoding="utf-8"))
    brows = backup.get("reviews", backup if isinstance(backup, list) else [])

    mgr = IFEDataManager(str(CACHE))
    cur = mgr.data.setdefault("reviews", [])
    by_url = {r.get("url"): r for r in cur if r.get("url")}

    added, enriched, skipped = [], [], []
    for b in brows:
        url = b.get("url")
        if not url:
            continue
        c = by_url.get(url)
        if c is None:
            if _passes_gate(b):
                cur.append(b); by_url[url] = b; added.append(b)
            else:
                skipped.append(b)
            continue
        if _richness(b) > _richness(c):
            for k in MERGE_FIELDS:
                if b.get(k) and not c.get(k):
                    c[k] = b[k]
            if _richness(b) > _richness(c):      # backup strictly better -> take its transcript set
                for k in ("transcript_available", "captions", "chapters"):
                    if b.get(k):
                        c[k] = b[k]
            enriched.append(c)

    print(f"backup rows: {len(brows)}  current rows: {len(by_url) - len(added)}")
    print(f"added: {len(added)}  enriched: {len(enriched)}  skipped (fail gate): {len(skipped)}")
    for r in added[:15]:
        print("  +", (r.get("title") or "")[:90])
    for r in skipped[:10]:
        print("  x", (r.get("title") or "")[:90])
    if dry:
        print("dry run - nothing written"); return
    if added or enriched:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        shutil.copy(CACHE, CACHE.with_name(f"ife_cache.pre-merge-{stamp}.json"))
        mgr.save_cache()
        print(f"saved {CACHE.name} ({len(mgr.data['reviews'])} rows); pre-merge copy kept as ife_cache.pre-merge-{stamp}.json")
    else:
        print("nothing to merge")


if __name__ == "__main__":
    main()
