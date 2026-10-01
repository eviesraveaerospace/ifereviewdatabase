"""
OCR-tag the internal (team) reviews' own photos and recordings, the way
gather_short_chapters.py tags YouTube Shorts, so every internal file carries
the ios-screen-recording-ocr screen-tag vocabulary and recordings get
timestamped chapters.

Why: the Forms import copied whatever tag the reviewer typed in the sheet.
Running the same tagger over the files standardizes the vocabulary and lets
the dashboard's Screens index jump to "Map" inside a recording at 3:40.

Per file (review["images"][i]):
  photo      tags      = sheet tags ∪ OCR tags (sheet tags kept in tags_sheet)
             ocr_tags, ocr_visual (True when the visual fallback decided)
  recording  chapters  = [{"t":"3:40","sec":220,"end":231,"title":"Map","tags":["Map"],"ife":true,"source":"ocr"}]
             tags      = sheet tags ∪ every chapter tag
  both       ocr_done  = ISO timestamp (skip on re-run; --force redoes)
Per review: screen_tags = union over files, ife_features merged, screen_tag_source="ocr-internal".

Run:  python tag_internal_media.py [--limit N] [--url URL] [--force] [--dry-run]
Needs the OCR checkout (IFE_OCR_REPO or the known paths in gather_short_chapters)
with rapidocr_onnxruntime + opencv in this interpreter. Local files only
(static/uploads/internal); SharePoint-only links are skipped.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
CACHE = HERE / "ife_cache.json"

# Reuse the Shorts chapterer's repo discovery, OCR → chapter pipeline and
# dashboard merge rules (tag-only chapters, ≤8 s gap merge for long media).
import gather_short_chapters as gsc  # noqa: E402  (inserts the OCR repo on sys.path)
from manual_review_tagger import predict_frame_tags, to_ife_features  # noqa: E402

OWN_ITEM = ("tags", "tags_sheet", "ocr_tags", "ocr_visual", "chapters", "ocr_done", "tags_verified")
OWN_REVIEW = ("screen_tags", "screen_tag_source", "ife_features")


def _local_path(item):
    web = item.get("local") or ""
    if not web.startswith("/static/"):
        return None
    p = HERE / web.lstrip("/")
    return p if p.exists() else None


def _orig_path(item, local):
    """Prefer the untouched original (orig/<key>.<ext>) when it is on disk —
    OCR reads small UI text better at full resolution."""
    orig_dir = local.parent / "orig"
    if orig_dir.exists():
        for p in orig_dir.glob(local.stem + ".*"):
            return p
    return local


def tag_photo(item, ocr, tagger):
    local = _local_path(item)
    if not local:
        return False
    tags, visual = predict_frame_tags(str(_orig_path(item, local)), ocr, tagger)
    sheet = item.get("tags_sheet") if "tags_sheet" in item else list(item.get("tags") or [])
    item["tags_sheet"] = sheet
    item["ocr_tags"] = tags
    item["ocr_visual"] = bool(visual)
    item["tags"] = _minus_removed(item, set(sheet) | set(tags))
    _verify(item)
    return True


def _verify(item):
    """Cross-reference: tags the reviewer typed in the sheet that the OCR
    also saw on the file (case-insensitive) — e.g. Map, Games. Stored as
    tags_verified; the dashboard marks them ✓."""
    sheet = {t.lower(): t for t in item.get("tags_sheet") or []}
    ocr = {t.lower() for t in item.get("ocr_tags") or []}
    item["tags_verified"] = sorted(sheet[k] for k in sheet if k in ocr)
    return item["tags_verified"]


def _minus_removed(item, tags):
    gone = {t.lower() for t in item.get("tags_removed") or []}
    return sorted(t for t in tags if t.lower() not in gone)


def tag_recording(item, ocr, interval):
    local = _local_path(item)
    if not local:
        return False
    with tempfile.TemporaryDirectory(prefix="internal_ocr_") as tmp:
        chaps = gsc.ocr_chapters(str(local), ocr, interval, os.path.join(tmp, "out"), long_form=True)
    sheet = item.get("tags_sheet") if "tags_sheet" in item else list(item.get("tags") or [])
    item["tags_sheet"] = sheet
    item["chapters"] = chaps
    gone = {t.lower() for t in item.get("tags_removed") or []}
    if gone:
        for c in chaps:
            c["tags"] = [t for t in c["tags"] if t.lower() not in gone]
            c["title"] = " · ".join(c["tags"])
        chaps = [c for c in chaps if c["tags"]]
        item["chapters"] = chaps
    item["ocr_tags"] = sorted({t for c in chaps for t in c["tags"]})
    item["tags"] = _minus_removed(item, set(sheet) | set(item["ocr_tags"]))
    _verify(item)
    return True


def report():
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = [r for r in data.get("reviews", []) if r.get("media_type") == "internal"]
    per_tag = {}          # sheet tag -> [agreed, total]
    extra = {}            # OCR tags the sheet never mentioned
    n_files = n_agree = 0
    for r in rows:
        for it in r.get("images") or []:
            if not it.get("ocr_done"):
                continue
            n_files += 1
            ver = {t.lower() for t in _verify(it)}
            if ver:
                n_agree += 1
            for t in it.get("tags_sheet") or []:
                c = per_tag.setdefault(t, [0, 0]); c[1] += 1; c[0] += t.lower() in ver
            for t in it.get("ocr_tags") or []:
                if t.lower() not in {x.lower() for x in it.get("tags_sheet") or []}:
                    extra[t] = extra.get(t, 0) + 1
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{n_files} OCR'd files; {n_agree} have at least one sheet tag the OCR confirmed")
    print()
    print("sheet tag                  confirmed / files")
    for t, (a, n) in sorted(per_tag.items(), key=lambda kv: -kv[1][1]):
        print(f"  {t:<24} {a:>4} / {n:<4} {'█' * int(10 * a / n) if n else ''}")
    print()
    print("OCR tags not in the sheet (most common):")
    for t, n in sorted(extra.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {t:<24} {n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (0 = all)")
    ap.add_argument("--url", help="only this review (internal://…)")
    ap.add_argument("--force", action="store_true", help="redo files that already have ocr_done")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between sampled frames in recordings")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="no OCR: recompute tags_verified from stored fields and print sheet-vs-OCR agreement per tag")
    a = ap.parse_args()
    if a.report:
        return report()

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = [r for r in data.get("reviews", []) if r.get("media_type") == "internal"]
    if a.url:
        rows = [r for r in rows if r.get("url") == a.url]
    todo = []
    for r in rows:
        for item in r.get("images") or []:
            if not _local_path(item):
                continue
            if item.get("ocr_done") and not a.force:
                continue
            todo.append((r, item))
    if a.limit:
        todo = todo[:a.limit]
    n_vid = sum(1 for _, it in todo if it.get("video"))
    print(f"OCR repo: {gsc.REPO}\n{len(todo)} files to tag ({n_vid} recordings) across {len({id(r) for r, _ in todo})} reviews")
    if a.dry_run or not todo:
        return

    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    try:
        from image_tagger import ImageTagger
        tagger = ImageTagger()
    except Exception as exc:  # noqa: BLE001  (open_clip / torch not installed here)
        print(f"visual fallback unavailable ({type(exc).__name__}: {str(exc)[:60]}); using text + globe detection only")

        class _NoVisual:
            def classify(self, path):
                return []
        tagger = _NoVisual()

    touched = {}

    def finish_review(r):
        tags = sorted({t for it in r.get("images") or [] for t in (it.get("tags") or [])})
        r["screen_tags"] = tags
        r["screen_tag_source"] = "ocr-internal"
        r["ife_features"] = to_ife_features(tags, r.get("ife_features") or {})
        touched[r["url"]] = r

    def save():
        # Merge, never overwrite: the app may have written the cache meanwhile.
        fresh = json.loads(CACHE.read_text(encoding="utf-8"))
        for row in fresh.get("reviews", []):
            src = touched.get(row.get("url"))
            if src is None:
                continue
            for k in OWN_REVIEW:
                if k in src:
                    row[k] = src[k]
            by_src = {it.get("src"): it for it in src.get("images") or []}
            for it in row.get("images") or []:
                s_it = by_src.get(it.get("src"))
                if s_it is None:
                    continue
                for k in OWN_ITEM:
                    if k in s_it:
                        it[k] = s_it[k]
        CACHE.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")

    t0, done, failed = time.time(), 0, 0
    for n, (r, item) in enumerate(todo, 1):
        name = (item.get("local") or "").rsplit("/", 1)[-1]
        print(f"[{n}/{len(todo)}] {r.get('title', '')[:50]}  {name}", end="  ", flush=True)
        try:
            ok = tag_recording(item, ocr, a.interval) if item.get("video") else tag_photo(item, ocr, tagger)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {type(exc).__name__}: {str(exc)[:80]}")
            failed += 1
            continue
        if not ok:
            print("skip (no local file)")
            continue
        item["ocr_done"] = datetime.now().isoformat(timespec="seconds")
        done += 1
        if item.get("video"):
            print(f"{len(item['chapters'])} chapters: " + ", ".join(f"{c['t']} {c['title']}" for c in item["chapters"][:6]))
        else:
            print(", ".join(item["ocr_tags"]) or "(no tag)" + ("  [visual]" if item.get("ocr_visual") else ""))
        finish_review(r)
        if n % 10 == 0:
            save()
    save()
    print(f"\nDone: {done} tagged, {failed} failed in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
