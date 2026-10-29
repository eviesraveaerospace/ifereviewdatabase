"""Prepare ios-screen-recording-ocr for a cloud OCR run (its .github/workflows/ocr_backlog.yml).

The OCR repo runs the job, so it needs: the two scripts the passes use (copied to cloud/),
and cloud/slim_cache.json - the public YouTube rows only, with just the fields the passes
read and finished work applied. Internal reviews, articles, uploads and notes are never
copied. Run it where the freshest cache is (the VM), then push the OCR clone:

    python export_cloud_ocr.py --ocr-clone ~/ife                       # writes cloud/ in the clone
    python export_cloud_ocr.py --ocr-clone ~/ife --cache ife_cache.json --extra-results ~/ife/cloud/ocr_results/cloud.json

The job's results come back as cloud/ocr_results/cloud.json in that repo; the nightly job
pulls the clone and applies them to the live cache (ocr_results.py).
"""
import argparse
import glob
import shutil
import sys
from pathlib import Path

import ocr_results

HERE = Path(__file__).parent
SCRIPTS = ("gather_short_chapters.py", "ocr_results.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr-clone", required=True, help="checkout of ios-screen-recording-ocr")
    ap.add_argument("--cache", default=str(ocr_results.CACHE))
    ap.add_argument("--out", default="", help="write the slim cache here instead of <clone>/cloud/slim_cache.json")
    a = ap.parse_args()
    clone = Path(a.ocr_clone).expanduser()
    if not (clone / "ocr_video_chapters.py").exists():
        sys.exit(f"{clone} is not an ios-screen-recording-ocr checkout")
    cloud = clone / "cloud"
    (cloud / "ocr_results").mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copy2(HERE / name, cloud / name)
    # Results the cloud job already produced are applied, so it does not redo those videos.
    done = sorted(glob.glob(str(cloud / "ocr_results" / "*.json")))
    out = Path(a.out) if a.out else cloud / "slim_cache.json"
    n = ocr_results.slim_cache(out, done, cache=Path(a.cache))
    print(f"exported {len(SCRIPTS)} scripts and a slim cache of {n} public YouTube rows "
          f"({out.stat().st_size / 1048576:.1f} MB; {len(done)} results file(s) applied) to {cloud}")


if __name__ == "__main__":
    main()
