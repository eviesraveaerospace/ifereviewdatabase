"""Resolve a git merge/rebase conflict in ife_cache.json by merging both sides by URL.

Called by nightly_transcripts.sh when `git pull --rebase` stops on the cache:
the cloud crawl appended rows upstream while this machine added transcripts
locally, and git cannot merge one big JSON array. We take the union of both
sides' reviews, keeping the richer copy of any URL present in both (same
rule as merge_cache_backup.py / IFEDataManager._dedupe_reviews), write the
result and `git add` it so the rebase can continue.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
CACHE = "ife_cache.json"


def _stage(n):
    out = subprocess.run(["git", "show", f":{n}:{CACHE}"], capture_output=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout.decode("utf-8"))


def _richness(r):
    return (bool(r.get("transcript_available")), len(r.get("captions") or []),
            len(r.get("chapters") or []), len(r.get("images") or []), len(json.dumps(r)))


def main():
    ours, theirs = _stage(2), _stage(3)
    if ours is None or theirs is None:
        print("resolve_cache_conflict: ife_cache.json is not in a conflicted state")
        return 1
    merged = {}
    for side in (ours, theirs):
        for r in side.get("reviews", []):
            u = r.get("url")
            if not u:
                continue
            if u not in merged or _richness(r) > _richness(merged[u]):
                merged[u] = r
    base = dict(ours)
    base["reviews"] = list(merged.values())
    base["last_updated"] = max(ours.get("last_updated") or "", theirs.get("last_updated") or "")
    Path(CACHE).write_text(json.dumps(base, indent=2, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "add", CACHE], check=True)
    print(f"resolve_cache_conflict: ours={len(ours.get('reviews', []))} theirs={len(theirs.get('reviews', []))} "
          f"merged={len(base['reviews'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
