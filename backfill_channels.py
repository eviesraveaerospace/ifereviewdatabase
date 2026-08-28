"""
Backfill the review cache with flight/cabin reviews from the known independent
reviewer channels (KNOWN_IFE_CHANNELS) that the keyword-gated daily crawl misses.

Trusted-channel videos pass a relaxed "aviation review" gate rather than the
strict IFE-keyword gate, so e.g. Sam Chui's "Emirates A380 First Class" reviews
get captured. Transcripts are NOT fetched here (run backfill_transcripts.py after).

Run:  python backfill_channels.py
Needs YOUTUBE_API_KEY (from .env).
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import ife_crawler
ife_crawler.TRANSCRIPTS_AVAILABLE = False  # skip transcript fetch during this pass
from ife_crawler import IFECrawler, KNOWN_IFE_CHANNELS

CACHE = Path(__file__).parent / "ife_cache.json"
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()


def main():
    if not KEY:
        print("ERROR: YOUTUBE_API_KEY not set.")
        sys.exit(1)

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    reviews = data.setdefault("reviews", [])
    existing = {r.get("url") for r in reviews}

    crawler = IFECrawler(verify_ssl=False, api_key=KEY)
    channels = {n: c for n, c in KNOWN_IFE_CHANNELS.items() if "(official)" not in n.lower()}

    added_total = 0
    for name, cid in channels.items():
        # Full-history backfill: page through the channel's entire uploads
        # playlist (1 API unit per 50 videos — even a 3,000-video channel
        # costs only ~60 units).
        ids = crawler._yt_search_channel(cid, limit=10**9)
        details = crawler._yt_fetch_details(ids)
        added = 0
        for vid_id in ids:
            url = f"https://www.youtube.com/watch?v={vid_id}"
            if url in existing:
                continue
            item = details.get(vid_id)
            if not item:
                continue
            entry = crawler._build_youtube_entry_from_api(vid_id, item, trusted=True)
            if entry:
                reviews.append(entry)
                existing.add(url)
                added += 1
        added_total += added
        print(f"  {name:<24} +{added} reviews")

    data["reviews"] = reviews
    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nAdded {added_total} channel reviews. Total now {len(reviews)}. Saved {CACHE.name}.")
    print("Run backfill_transcripts.py next to fetch transcripts for the new videos.")


if __name__ == "__main__":
    main()
