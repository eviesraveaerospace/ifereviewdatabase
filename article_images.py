"""Export the pictures from press articles and tag them with the IFE screen pipeline.

Usage:
  python article_images.py                 # fetch images for articles that have none, then tag them
  python article_images.py --limit 5       # first N articles only
  python article_images.py --retag         # re-run tagging on already-exported images
  python article_images.py --no-tag        # download only

Images are saved under static/article_images/ (git-ignored; each machine
exports its own copy) and recorded on the review row as:

  "images": [{"src": <publisher url>, "local": "/static/article_images/<hash>.jpg",
              "alt": ..., "caption": ..., "airline": "Emirates", "ife_system": "Emirates ICE",
              "tags": ["Media Navigation", "Map"], "used_visual": false}]
  "screen_tags": [...]            # article-level union, same vocabulary as employee submissions
  "screen_tag_source": "auto"
  "ife_features": {...}           # dashboard features merged via the tagger's FEATURE_MAP

Tagging runs the ios-screen-recording-ocr project's manual_review_tagger.py
out-of-process in its own venv, exactly like dashboard/manual_review_worker.py
does for employee submissions (OCR -> keyword tags -> fallback trigger ->
globe detector -> CLIP visual classifier). Configure with:

  TAGGER_PYTHON   default ~/ife-venv/bin/python
  TAGGER_SCRIPT   default ~/ife/manual_review_tagger.py

If the tagger is not installed on this machine, --light falls back to the
text-only stage (RapidOCR + roi_ocr.infer_tags) and marks the source as
"text-only" so it can be redone on a host with the full pipeline.

The airline is read from the image's alt/caption (and OCR text in light
mode) against the crawler's airline list, falling back to the article's own
airline mentions; the IFE system comes from the OCR project's
ife_systems.json registry (airline + airframe) or the crawler's lookup.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib3
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ife_crawler import IFECrawler, AIRLINE_KEYWORDS, AIRLINE_IFE_LOOKUP, _keyword_re

urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
CACHE = ROOT / "ife_cache.json"
IMG_DIR = ROOT / "static" / "article_images"
MAX_PER_ARTICLE = 12
MIN_BYTES = 12_000
TAG_TIMEOUT = 900

TAGGER_PYTHON = os.environ.get("TAGGER_PYTHON", os.path.expanduser("~/ife-venv/bin/python"))
TAGGER_SCRIPT = os.environ.get("TAGGER_SCRIPT", os.path.expanduser("~/ife/manual_review_tagger.py"))

_SKIP_URL_RE = re.compile(
    r"logo|icon|avatar|sprite|badge|pixel|tracking|gravatar|emoji|button|share|banner|"
    r"placeholder|blank\.|spacer|/ads?/|advert|widget|favicon|thumb_up|rating|author|profile|"
    r"\.svg(\?|$)|\.gif(\?|$)|^data:", re.I)


# ── Image discovery / export ─────────────────────────────────────────────────
def _pick_srcset(srcset: str) -> str:
    best, best_w = "", -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                w = 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


def _int(v):
    try:
        return int(str(v).rstrip("px"))
    except (TypeError, ValueError):
        return None


def collect_image_urls(soup: BeautifulSoup, base_url: str):
    """Ordered, de-duplicated candidate images from the article body (lead image first)."""
    found, seen = [], set()

    def add(u, alt="", caption="", w=None, h=None):
        if not u or _SKIP_URL_RE.search(u):
            return
        u = urljoin(base_url, u.strip())
        if not u.startswith("http"):
            return
        key = u.split("?")[0]
        if key in seen or (w and w < 220) or (h and h < 160):
            return
        seen.add(key)
        found.append({"src": u, "alt": (alt or "").strip()[:200], "caption": (caption or "").strip()[:300]})

    og = soup.find("meta", {"property": "og:image"}) or soup.find("meta", {"name": "twitter:image"})
    if og and og.get("content"):
        add(og["content"], alt=(soup.title.string or "").strip() if soup.title and soup.title.string else "")

    root = soup.find("article") or soup.find("main") or soup.body or soup
    for t in root.find_all(["nav", "footer", "aside", "form", "header"]):
        t.decompose()
    for img in root.find_all("img"):
        u = ""
        for k in ("data-src", "data-lazy-src", "data-original", "src"):
            if img.get(k) and not str(img.get(k)).startswith("data:"):
                u = img[k]; break
        ss = img.get("data-srcset") or img.get("srcset")
        if ss:
            u = _pick_srcset(ss) or u
        cap = ""
        fig = img.find_parent("figure")
        if fig is not None:
            fc = fig.find("figcaption")
            if fc:
                cap = fc.get_text(" ", strip=True)
        add(u, alt=img.get("alt", ""), caption=cap, w=_int(img.get("width")), h=_int(img.get("height")))
        if len(found) >= MAX_PER_ARTICLE:
            break
    return found[:MAX_PER_ARTICLE]


def download(url: str, session: requests.Session):
    """Save the image locally; return the /static path or None."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ext
    dest = IMG_DIR / name
    if dest.exists() and dest.stat().st_size >= MIN_BYTES:
        return "/static/article_images/" + name
    resp = session.get(url, timeout=20, verify=False, headers={"Referer": url})
    resp.raise_for_status()
    if not resp.headers.get("Content-Type", "").startswith("image/") or len(resp.content) < MIN_BYTES:
        return None
    dest.write_bytes(resp.content)
    return "/static/article_images/" + name


# ── Tagging ──────────────────────────────────────────────────────────────────
def _display_airline(kw: str) -> str:
    special = {"ana": "ANA", "jal": "Japan Airlines", "klm": "KLM", "tap air portugal": "TAP Air Portugal",
               "eva air": "EVA Air", "swiss": "SWISS"}
    return special.get(kw, kw.title())


def airline_from(*texts: str):
    blob = " ".join(t for t in texts if t).lower()
    best, best_n = None, 0
    for kw in AIRLINE_KEYWORDS:
        n = len(_keyword_re(kw).findall(blob))
        if n > best_n:
            best, best_n = kw, n
    return best


class SystemRegistry:
    """airline + airframe -> IFE system, from the OCR project's ife_systems.json."""

    def __init__(self, repo_dir: Path):
        reg = repo_dir / "ife_systems.json"
        self.systems = json.loads(reg.read_text(encoding="utf-8")) if reg.exists() else {}
        self._by_lower = {k.lower(): k for k in self.systems if not k.startswith("_")}

    def lookup(self, airline_kw, airframes):
        if not airline_kw:
            return None
        reg = self.systems.get(self._by_lower.get(_display_airline(airline_kw).lower(), ""), {})
        for af in airframes:
            key = af.upper().replace(" ", "").replace("-", "")
            for rk, rv in reg.items():
                if rk.upper().replace(" ", "").replace("-", "").startswith(key):
                    return short_system(rv.get("vendor", ""), rv.get("system", ""))
        return AIRLINE_IFE_LOOKUP.get(airline_kw)


def short_system(vendor: str, system: str):
    """Chip-sized system name. Registry entries carry research notes in the system field
    ('eX3 (KrisWorld) on long-haul/ULR; Thales AVANT on regional subfleet'); keep the
    leading product name only, and the vendor's first word (Panasonic, Thales, Safran)."""
    name = re.split(r"\s*[;(/]|\s+on\s+|\s+\(", system or "", maxsplit=1)[0].strip(" -")
    vend = (vendor or "").split()[0] if vendor else ""
    if vend and name.lower().startswith(vend.lower()):
        vend = ""
    out = f"{vend} {name}".strip()
    return out or None


class FullTagger:
    """manual_review_tagger.py in its own venv (same contract as manual_review_worker._tag)."""
    source = "auto"

    def __init__(self):
        if not (os.path.exists(TAGGER_PYTHON) and os.path.exists(TAGGER_SCRIPT)):
            raise RuntimeError(f"tagger not found (TAGGER_PYTHON={TAGGER_PYTHON}, TAGGER_SCRIPT={TAGGER_SCRIPT})")
        self.repo = Path(TAGGER_SCRIPT).parent

    def tag(self, paths, text):
        cmd = [TAGGER_PYTHON, TAGGER_SCRIPT, *paths]
        if text:
            cmd += ["--text", text[:2000]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TAG_TIMEOUT,
                              env={**os.environ, "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1")})
        if proc.returncode != 0:
            print(f"    tagger failed ({proc.returncode}): {proc.stderr.strip()[-400:]}", file=sys.stderr)
            return None
        try:
            return json.loads(proc.stdout[proc.stdout.index("{"):])
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"    unreadable tagger output: {exc}", file=sys.stderr)
            return None


class LightTagger:
    """Text-only stage of the pipeline, in-process, for hosts without torch/CLIP."""
    source = "text-only"

    def __init__(self):
        cands = [os.environ.get("IFE_OCR_REPO", ""), str(Path(TAGGER_SCRIPT).parent),
                 str(Path.home() / "ife"), "C:/workspace/ios-screen-recording-ocr"]
        self.repo = next((Path(c) for c in cands if c and (Path(c) / "roi_ocr.py").exists()), None)
        if self.repo is None:
            raise RuntimeError("ios-screen-recording-ocr checkout not found; set IFE_OCR_REPO")
        sys.path.insert(0, str(self.repo))
        from rapidocr_onnxruntime import RapidOCR  # noqa: WPS433
        import roi_ocr  # noqa: WPS433
        self._ocr, self._infer = RapidOCR(), roi_ocr.infer_tags
        self.ocr_text = {}

    def tag(self, paths, text):
        frames, counts = [], {}
        for p in paths:
            try:
                result, _ = self._ocr(str(p))
            except Exception as exc:  # noqa: BLE001
                print(f"    OCR failed ({exc})"); result = None
            t = "\n".join(s.strip() for _, s, _ in (result or []) if s and s.strip())
            self.ocr_text[os.path.basename(p)] = t
            tags = sorted(self._infer(t)[0]) if t.strip() else []
            for tg in set(tags):
                counts[tg] = counts.get(tg, 0) + 1
            frames.append({"frame": os.path.basename(p), "tags": tags, "used_visual": False, "is_still": True})
        return {"screen_tags": sorted(counts), "screen_tag_frames": frames, "screen_tag_counts": counts,
                "ife_features": {}}


_AIRLINE_OCR = {}


def screen_text(tagger, path: Path) -> str:
    """On-screen text for airline detection. The light tagger already OCR'd it; the full
    tagger returns tags only, so read the text here if RapidOCR is importable (optional)."""
    cached = getattr(tagger, "ocr_text", {}).get(path.name)
    if cached is not None:
        return cached
    if "ocr" not in _AIRLINE_OCR:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: WPS433
            _AIRLINE_OCR["ocr"] = RapidOCR()
        except Exception:  # noqa: BLE001
            _AIRLINE_OCR["ocr"] = None
    ocr = _AIRLINE_OCR["ocr"]
    if ocr is None:
        return ""
    try:
        result, _ = ocr(str(path))
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(t.strip() for _, t, _ in (result or []) if t and t.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retag", action="store_true", help="re-run tagging on exported images")
    ap.add_argument("--no-tag", action="store_true", help="download only")
    ap.add_argument("--light", action="store_true", help="text-only tagging when the full tagger is absent")
    args = ap.parse_args()

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    arts = [r for r in data.get("reviews", []) if r.get("media_type") == "article"]
    if args.limit:
        arts = arts[:args.limit]
    session = requests.Session()
    session.headers.update(IFECrawler.HEADERS)

    tagger = registry = None
    if not args.no_tag:
        try:
            tagger = FullTagger()
        except RuntimeError as exc:
            print(exc)
            if args.light:
                tagger = LightTagger()
        if tagger:
            registry = SystemRegistry(tagger.repo)
            print(f"tagger: {tagger.source} ({tagger.repo})")
        else:
            print("tagging skipped: pass --light for the text-only stage, or run on the tagger host")

    fetched = tagged = failed = 0
    for r in arts:
        url = r["url"]
        if "images" not in r:   # [] means "checked, none usable" and is not re-fetched
            try:
                resp = session.get(url, timeout=20, verify=False)
                resp.raise_for_status()
                if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = resp.apparent_encoding
                imgs = []
                for c in collect_image_urls(BeautifulSoup(resp.text, "html.parser"), resp.url):
                    try:
                        local = download(c["src"], session)
                    except Exception:  # noqa: BLE001
                        local = None
                    if local:
                        imgs.append({**c, "local": local})
                r["images"] = imgs
                fetched += 1
                print(f"  {len(imgs):2d} images  {url[:80]}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAIL {type(exc).__name__}: {str(exc)[:60]}  {url[:80]}")
                continue

        if tagger is None:
            continue
        todo = [im for im in r.get("images", [])
                if (args.retag or im.get("tags") is None or r.get("screen_tag_source") == "text-only" and tagger.source == "auto")
                and (ROOT / im["local"].lstrip("/")).exists()]
        if not todo:
            continue
        paths = [str(ROOT / im["local"].lstrip("/")) for im in todo]
        result = tagger.tag(paths, r.get("transcript_excerpt", ""))
        if result is None:
            continue
        by_frame = {f["frame"]: f for f in result.get("screen_tag_frames", [])}
        # The article's own airline is only a safe fallback when the piece is about one airline;
        # a round-up ("best IFE of 5 airlines") would mislabel every uncaptioned photo.
        mentioned = [m["keyword"] for m in r.get("airlines_mentioned", []) if m.get("keyword")]
        art_airline = mentioned[0] if len(mentioned) == 1 else None
        airframes = [m["keyword"] for m in r.get("aircraft_mentioned", []) if m.get("keyword")]
        for im in todo:
            fr = by_frame.get(os.path.basename(im["local"]), {})
            ocr_txt = screen_text(tagger, ROOT / im["local"].lstrip("/"))
            kw = airline_from(im.get("alt", ""), im.get("caption", ""), ocr_txt)
            src = "image" if kw else ("article" if art_airline else None)
            kw = kw or art_airline
            im["tags"] = fr.get("tags", [])
            im["used_visual"] = bool(fr.get("used_visual"))
            im["airline"] = _display_airline(kw) if kw else None
            im["airline_source"] = src
            im["ife_system"] = (registry.lookup(kw, airframes) if kw else None) or (
                r.get("ife_system") or r.get("ife_system_guess") if src == "article" else None)
            tagged += 1
            print(f"    · {Path(im['local']).name}  {im['airline'] or '-'} | {', '.join(im['tags']) or 'no tag'}")
        r["screen_tags"] = result.get("screen_tags", [])
        r["screen_tag_source"] = tagger.source
        r["screen_tag_counts"] = result.get("screen_tag_counts", {})
        feats = dict(r.get("ife_features") or {})
        feats.update(result.get("ife_features") or {})
        r["ife_features"] = feats

    CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\narticles fetched: {fetched}  images tagged: {tagged}  failed: {failed}. Saved {CACHE.name}.")


if __name__ == "__main__":
    main()
