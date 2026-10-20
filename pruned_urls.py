"""URLs listed in pruned_offtopic.json: entries prune_offtopic.py removed on purpose.

Everything that adds or merges cache rows (crawl, discovery backfill, the
conflict resolver) consults this so a pruned video does not come back
untagged the next night. To restore one, delete it from pruned_offtopic.json.
"""
import json
from pathlib import Path

PRUNED = Path(__file__).parent / "pruned_offtopic.json"


def load(path=PRUNED):
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {i.get("url") for i in items if isinstance(i, dict) and i.get("url")}


def drop_pruned(rows, pruned=None):
    """rows without pruned YouTube entries; team-entered (internal) rows are never dropped."""
    pruned = load() if pruned is None else pruned
    return [r for r in rows if r.get("media_type") == "internal" or r.get("url") not in pruned]
