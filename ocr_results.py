"""OCR results a second machine computed, and how they get into the cache.

gather_short_chapters.py --results-out FILE writes, per URL, the fields an OCR pass owns
(OWN) into a small JSON instead of the 55 MB LFS cache, so a laptop can read videos
without committing the cache or resolving cache conflicts. The machine that owns the live
cache (the VM) runs

    python ocr_results.py                  # applies ocr_results/*.json to ife_cache.json
    python ocr_results.py FILE [FILE ...]

under the same lock the OCR shards save with. A row already read at the same or a newer
tagger version with chapters and tags keeps what it has.
"""
import contextlib
import glob
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "ife_cache.json"

# What an OCR pass owns on a row.
OWN = ("chapters", "chapters_source", "chapters_ocr_status", "screen_tags", "screen_tag_source", "ife_features",
       "ife_tags", "ife_tags_status", "ife_tags_scope", "ocr_tagger_v")


@contextlib.contextmanager
def cache_lock(path=CACHE):
    """Serialise writes to `path` between parallel shards (flock on POSIX, msvcrt on Windows)."""
    lock = Path(path).with_suffix(".lock")
    with open(lock, "a+") as fh:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
            release = lambda: fcntl.flock(fh, fcntl.LOCK_UN)  # noqa: E731
        except ImportError:
            import msvcrt
            fh.seek(0)
            while True:                       # LK_LOCK gives up after ~10 s; keep waiting
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
            release = lambda: (fh.seek(0), msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1))  # noqa: E731
        try:
            yield
        finally:
            release()


def load_results(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_results(path, touched):
    """Merge touched rows' OCR fields into the results file (url -> fields), atomically."""
    out = Path(path)
    with cache_lock(out):
        res = load_results(out)
        for url, src in touched.items():
            res[url] = {k: src[k] for k in OWN if k in src}
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out)


def apply_results(rows, results):
    """Overlay a results dict on rows (in memory). A row already read at the same or a newer
    tagger version with chapters and tags keeps what it has. Returns how many were applied."""
    n = 0
    for row in rows:
        res = results.get(row.get("url"))
        if not res:
            continue
        done = bool(row.get("chapters")) and bool(row.get("screen_tags") or row.get("ife_tags"))
        if done and (row.get("ocr_tagger_v") or 0) >= (res.get("ocr_tagger_v") or 0):
            continue
        for k in OWN:
            if k in res:
                row[k] = res[k]
            else:
                row.pop(k, None)
        n += 1
    return n


def merge_into_cache(files, cache=CACHE):
    with cache_lock(cache):
        data = json.loads(Path(cache).read_text(encoding="utf-8"))
        rows = data.get("reviews", [])
        n = sum(apply_results(rows, load_results(f)) for f in files)
        if n:
            tmp = Path(cache).with_suffix(".mergetmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cache)
    return n


def main(argv):
    files = argv or sorted(glob.glob(str(HERE / "ocr_results" / "*.json")))
    if not files:
        print("ocr_results: no results files")
        return 0
    n = merge_into_cache(files)
    print(f"ocr_results: applied {n} rows from {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
