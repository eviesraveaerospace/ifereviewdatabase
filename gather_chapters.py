"""
Gather YouTube chapters for cached videos by parsing timestamped lines from the
video description (via the Data API — not IP-blocked). Flags IFE-related chapters
so the app can offer a jump-to-IFE control.

Stores on each review:  chapters = [{"t":"5:30","sec":330,"title":"...","ife":true}]

Run:  python gather_chapters.py     (needs YOUTUBE_API_KEY)
"""
import json
import os
import re
import sys
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass  # windowless pythonw has no stdout to reconfigure

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

API = "https://www.googleapis.com/youtube/v3/videos"
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
CACHE = Path(__file__).parent / "ife_cache.json"

_TS_RE = re.compile(r"^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:.)]?\s*(.+?)\s*$")
_IFE_CHAPTER_TERMS = (
    "ife", "in-flight entertainment", "inflight entertainment", "in flight entertainment",
    "entertainment", "screen", "seatback", "seat back", "movies", "tv ", "tv/",
    "wifi", "wi-fi", "bluetooth", "4k", "oled", "content", "avod", "system",
)


def _to_seconds(ts):
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def parse_chapters(desc):
    """Extract chapters from a description. YouTube requires the first at 0:00 and
    at least 3 timestamps — we apply the same rule to avoid false positives."""
    chaps = []
    for line in (desc or "").splitlines():
        m = _TS_RE.match(line)
        if not m:
            continue
        ts, title = m.group(1), m.group(2).strip()
        if not title or len(title) > 90:
            continue
        sec = _to_seconds(ts)
        low = title.lower()
        ife = any(t in low for t in _IFE_CHAPTER_TERMS)
        chaps.append({"t": ts, "sec": sec, "title": title, "ife": ife})
    if len(chaps) < 3 or chaps[0]["sec"] != 0:
        return []
    # de-dupe / keep ascending
    out, last = [], -1
    for c in chaps:
        if c["sec"] > last:
            out.append(c)
            last = c["sec"]
    return out


def main():
    if not KEY:
        print("ERROR: YOUTUBE_API_KEY not set.")
        sys.exit(1)
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    reviews = data.get("reviews", [])
    by_id = {}
    for r in reviews:
        u = r.get("url", "")
        if "youtube.com/watch?v=" in u:
            by_id.setdefault(u.split("watch?v=")[1].split("&")[0], []).append(r)
    ids = list(by_id)
    print(f"Fetching descriptions for {len(ids)} videos…")
    with_ch = with_ife = 0
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        resp = requests.get(API, params={"part": "snippet", "id": ",".join(batch), "key": KEY},
                            timeout=20, verify=False)
        if resp.status_code != 200:
            print(f"  batch {i//50} failed: {resp.status_code}")
            continue
        for item in resp.json().get("items", []):
            desc = item.get("snippet", {}).get("description", "")
            chaps = parse_chapters(desc)
            for r in by_id.get(item["id"], []):
                # OCR-derived chapters (gather_short_chapters.py) stand in when the
                # description has none — never wipe them with an empty list.
                if not chaps and r.get("chapters_source") == "ocr":
                    continue
                r["chapters"] = chaps
                r["chapters_source"] = "description" if chaps else None
                if chaps:
                    with_ch += 1
                    if any(c["ife"] for c in chaps):
                        with_ife += 1
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Done. {with_ch} videos have chapters; {with_ife} have an IFE-related chapter. Saved {CACHE.name}.")


if __name__ == "__main__":
    main()
