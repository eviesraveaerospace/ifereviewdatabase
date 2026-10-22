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

import pruned_urls

sys.stdout.reconfigure(encoding="utf-8")
CACHE = "ife_cache.json"


def _stage(n):
    out = subprocess.run(["git", "show", f":{n}:{CACHE}"], capture_output=True)
    if out.returncode != 0:
        return None
    blob = out.stdout
    # The cache is in Git LFS: the index holds a pointer; smudge it to content.
    if blob.startswith(b"version https://git-lfs"):
        blob = subprocess.run(["git", "lfs", "smudge"], input=blob, capture_output=True, check=True).stdout
    return json.loads(blob.decode("utf-8"))


def _richness(r):
    return (bool(r.get("transcript_available")), len(r.get("captions") or []),
            len(r.get("chapters") or []), len(r.get("images") or []), len(json.dumps(r)))


# OCR results one machine computed must survive the other machine winning the
# row on transcript/captions (two machines now run the IFE-chapter pass).
_OCR_KEYS = ("ife_tags", "ife_tags_status", "ife_tags_scope", "ocr_tagger_v", "chapters_ocr_status")


def _carry_ocr(win, lose):
    newer = (lose.get("ocr_tagger_v") or 0) > (win.get("ocr_tagger_v") or 0)
    if lose.get("ife_tags_status") and (not win.get("ife_tags_status") or newer):
        for k in _OCR_KEYS:
            if k in lose:
                win[k] = lose[k]
            elif newer:
                win.pop(k, None)
        tags = sorted(set(win.get("screen_tags") or []) | set(lose.get("screen_tags") or []))
        if tags:
            win["screen_tags"] = tags
            if "screen_tag_source" in lose:
                win.setdefault("screen_tag_source", lose["screen_tag_source"])
        feats = dict(lose.get("ife_features") or {})
        feats.update(win.get("ife_features") or {})
        if feats:
            win["ife_features"] = feats
    if lose.get("chapters_ocr_status") and not win.get("chapters_ocr_status") and not win.get("chapters"):
        win["chapters_ocr_status"] = lose["chapters_ocr_status"]


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
            if u not in merged:
                merged[u] = r
                continue
            win, lose = (r, merged[u]) if _richness(r) > _richness(merged[u]) else (merged[u], r)
            _carry_ocr(win, lose)
            merged[u] = win
    # Rows the prune removed on purpose must not come back from the other side.
    kept = pruned_urls.drop_pruned(merged.values())
    dropped = len(merged) - len(kept)
    merged = {r["url"]: r for r in kept}
    base = dict(ours)
    base["reviews"] = list(merged.values())
    base["last_updated"] = max(ours.get("last_updated") or "", theirs.get("last_updated") or "")
    Path(CACHE).write_text(json.dumps(base, indent=2, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "add", CACHE], check=True)
    print(f"resolve_cache_conflict: ours={len(ours.get('reviews', []))} theirs={len(theirs.get('reviews', []))} "
          f"merged={len(base['reviews'])} (pruned rows kept out: {dropped})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
