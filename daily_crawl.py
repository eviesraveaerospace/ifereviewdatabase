import os
import sys
import logging
from pathlib import Path
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from ife_crawler import IFECrawler
from ife_data_manager import IFEDataManager

LOG_FILE = Path(__file__).parent / "crawl_log.txt"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main():
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        logging.error("YOUTUBE_API_KEY not set — aborting")
        print("ERROR: YOUTUBE_API_KEY not set. Add it to the .env file.")
        sys.exit(1)

    logging.info("Daily crawl starting")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Daily crawl starting")

    data_manager = IFEDataManager()
    data_manager.reload_from_disk()
    existing = {r["url"] for r in data_manager.data.get("reviews", [])}
    before = len(existing)

    # 30 days (was 14): one missed week no longer loses videos for good.
    days_lookback = int(os.environ.get("DAYS_LOOKBACK", "30"))
    crawler = IFECrawler(verify_ssl=False, api_key=api_key)
    results = crawler.auto_discover(existing_urls=existing, max_results=2000, days_lookback=days_lookback)

    if results:
        data_manager.reload_from_disk()
        data_manager.data["reviews"].extend(results)
        data_manager.save_cache()
        after = len(data_manager.data["reviews"])
        msg = f"Added {len(results)} new results (total: {after}, was: {before})"
    else:
        msg = "No new results found"
    if crawler.quota_exhausted:
        msg += f"  ** YOUTUBE QUOTA EXHAUSTED after {crawler.api_calls} API calls — results are incomplete ({crawler.api_error}) **"
    elif crawler.api_error:
        msg += f"  (last API error: {crawler.api_error})"
    logging.info(msg)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


if __name__ == "__main__":
    main()
