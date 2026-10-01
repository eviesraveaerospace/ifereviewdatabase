"""
Transcribe the internal (team) reviews' own recordings with local Whisper,
the same model the nightly YouTube backfill uses (WHISPER_MODEL, default base).

Screen recordings are often silent or carry only cabin noise; Whisper then
returns nothing useful and the file is marked so it is not retried. When a
reviewer narrates, the text becomes searchable like any crawled transcript.

Per recording (review["images"][i] with video=True and a local file):
  transcript = {"text": "...", "lang": "en", "segments": [{"t":"0:12","sec":12,"text":"..."}]}
  transcript_done = ISO timestamp (skip on re-run; --force redoes)
Per review: transcript_full = narration of every recording (only if any),
            transcript_available, transcript_source = "whisper-internal".

Run:  python transcribe_internal.py [--limit N] [--url URL] [--force] [--dry-run]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

HERE = Path(__file__).parent
CACHE = HERE / "ife_cache.json"
MIN_WORDS = 6          # fewer real words than this → treated as silent
OWN_ITEM = ("transcript", "transcript_done")
OWN_REVIEW = ("transcript_full", "transcript_available", "transcript_source")


def _local_path(item):
    web = item.get("local") or ""
    p = HERE / web.lstrip("/")
    return p if web.startswith("/static/") and p.exists() else None


def fmt_ts(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def transcribe(model, path):
    res = model.transcribe(str(path), fp16=False)
    segs = [{"t": fmt_ts(s["start"]), "sec": int(round(s["start"])), "text": s["text"].strip()}
            for s in res.get("segments", []) if s.get("text", "").strip()]
    text = " ".join(s["text"] for s in segs).strip()
    if len(text.split()) < MIN_WORDS:
        return {"text": "", "lang": res.get("language"), "segments": [], "silent": True}
    return {"text": text, "lang": res.get("language"), "segments": segs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--url")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = [r for r in data.get("reviews", []) if r.get("media_type") == "internal"]
    if a.url:
        rows = [r for r in rows if r.get("url") == a.url]
    todo = [(r, it) for r in rows for it in r.get("images") or []
            if it.get("video") and _local_path(it) and (a.force or not it.get("transcript_done"))]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} recordings to transcribe across {len({id(r) for r, _ in todo})} reviews")
    if a.dry_run or not todo:
        return

    import whisper
    model = whisper.load_model(os.environ.get("WHISPER_MODEL", "base").strip() or "base")

    touched = {}

    def finish(r):
        texts = [it["transcript"]["text"] for it in r.get("images") or []
                 if (it.get("transcript") or {}).get("text")]
        if texts:
            r["transcript_full"] = "\n".join(texts)
            r["transcript_available"] = True
            r["transcript_source"] = "whisper-internal"
        touched[r["url"]] = r

    def save():
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
                if s_it:
                    for k in OWN_ITEM:
                        if k in s_it:
                            it[k] = s_it[k]
        CACHE.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")

    t0, done, silent, failed = time.time(), 0, 0, 0
    for n, (r, it) in enumerate(todo, 1):
        name = (it.get("local") or "").rsplit("/", 1)[-1]
        print(f"[{n}/{len(todo)}] {r.get('title', '')[:50]}  {name}", end="  ", flush=True)
        try:
            it["transcript"] = transcribe(model, _local_path(it))
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {type(exc).__name__}: {str(exc)[:80]}")
            failed += 1
            continue
        it["transcript_done"] = datetime.now().isoformat(timespec="seconds")
        done += 1
        if it["transcript"].get("silent"):
            silent += 1
            print("(no speech)")
        else:
            print(f"{len(it['transcript']['segments'])} segments: {it['transcript']['text'][:70]}…")
        finish(r)
        if n % 5 == 0:
            save()
    save()
    print(f"\nDone: {done} transcribed ({silent} silent), {failed} failed in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
