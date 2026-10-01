"""
Localize internal-review media: download the SharePoint screenshots that the
"Airline IFE Market Research Database.xlsx" import stored as external links,
so the dashboard shows the image instead of a "sign-in required" card.

Downloads go through Microsoft Graph's sharing-link API (/shares/{id}/driveItem),
which resolves the `https://<tenant>.sharepoint.com/:i:/s/...` links directly.
Auth: put a browser cookie export (Netscape cookies.txt with sharepoint.com
cookies) at cookies_sharepoint.txt and the script uses that session — no consent
flow needed. Otherwise it falls back to a delegated device-code sign-in (MSAL,
needs tenant admin approval here); that token is cached in
.msal_token_cache.json (git-ignored) so subsequent runs — and the /api/import-forms
route — need no interaction.

  python internal_media.py            # download every still-external image
  python internal_media.py --dry-run  # list what would be fetched
  python internal_media.py --login    # just (re)authenticate

Env: MS_GRAPH_CLIENT_ID (defaults to the Microsoft Graph PowerShell public
client), MS_GRAPH_TENANT (default: organizations).
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).parent
CACHE = HERE / "ife_cache.json"
MEDIA_DIR = HERE / "static" / "uploads" / "internal"
TOKEN_CACHE = HERE / ".msal_token_cache.json"
FLOW_FILE = HERE / ".msal_device_flow.json"
COOKIE_FILE = HERE / "cookies_sharepoint.txt"   # browser cookie export (git-ignored)
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.Read.All", "Sites.Read.All"]
# Microsoft Graph PowerShell's public client: preauthorized for Graph delegated
# scopes, so device-code sign-in works without an app registration. (Azure CLI's
# client is refused with AADSTS65002 for Graph.) Override with MS_GRAPH_CLIENT_ID.
CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "14d82eec-204b-4c2f-b7e8-296a70dab67e")
TENANT = os.environ.get("MS_GRAPH_TENANT", "organizations")

_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                "image/heic": ".heic", "video/mp4": ".mp4", "video/quicktime": ".mov"}


def is_sharepoint(url: str) -> bool:
    u = (url or "").lower()
    return "sharepoint.com/" in u or "1drv.ms/" in u or "onedrive.live.com/" in u


# ── auth ─────────────────────────────────────────────────────────────────────
def _msal_app():
    import msal
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE.exists():
        cache.deserialize(TOKEN_CACHE.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT}",
                                       token_cache=cache)
    return app, cache


def _persist(cache):
    if cache.has_state_changed:
        TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")


def get_token(interactive: bool = True):
    """Access token for Graph. Silent from cache when possible; otherwise a
    device-code prompt (only when `interactive`). Returns None if unavailable."""
    app, cache = _msal_app()
    accounts = app.get_accounts()
    if accounts:
        res = app.acquire_token_silent(SCOPES, account=accounts[0])
        if res and "access_token" in res:
            _persist(cache)
            return res["access_token"]
    if not interactive:
        return None
    flow = None
    if FLOW_FILE.exists():  # a code was already issued by --login-start; keep polling that one
        try:
            flow = json.loads(FLOW_FILE.read_text(encoding="utf-8"))
            if flow.get("expires_at", 0) < time.time() + 30:
                flow = None
        except ValueError:
            flow = None
    if flow is None:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"device flow failed: {flow.get('error_description') or flow}")
        print("\n" + flow["message"] + "\n", flush=True)
    res = app.acquire_token_by_device_flow(flow)
    FLOW_FILE.unlink(missing_ok=True)
    if "access_token" not in res:
        raise SystemExit(f"sign-in failed: {res.get('error_description') or res}")
    _persist(cache)
    return res["access_token"]


# ── download ─────────────────────────────────────────────────────────────────
def _share_id(link: str) -> str:
    b = base64.urlsafe_b64encode(link.encode("utf-8")).decode("ascii").rstrip("=")
    return "u!" + b


def cookie_session(path: Path) -> requests.Session | None:
    """A requests session carrying a browser's SharePoint cookies (Netscape cookies.txt
    export, e.g. from the 'Get cookies.txt LOCALLY' Edge/Chrome extension)."""
    import http.cookiejar
    if not path or not Path(path).exists():
        return None
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    # Browser exports write session cookies (FedAuth, rtFa) with expiry 0, which
    # the cookiejar then treats as already expired and never sends. Make them
    # true session cookies.
    for c in jar:
        if not c.expires:
            c.expires = None
    if not any("sharepoint" in (c.domain or "") for c in jar):
        print(f"{path}: no sharepoint.com cookies in this file")
        return None
    s = requests.Session()
    s.cookies = jar
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
    return s


def get_auth(interactive: bool = False, cookies: str | Path | None = None):
    """Whatever auth is available: a cookie session (preferred when a cookie file
    exists), else a cached/interactive Graph token, else None."""
    sess = cookie_session(Path(cookies) if cookies else COOKIE_FILE)
    if sess is not None:
        return sess
    return get_token(interactive=interactive)


# The screenshot tree was moved after the Excel links were written. Old prefix →
# new prefix; the airline/class/airframe tail is unchanged (a trailing year
# folder may or may not survive, so both are tried).
PATH_REMAPS = [
    ("/Internal Documentation/Airline Auxiliary IFE Research Database/Airlines/",
     "/Shared Documents/Airlines IFE GUI Assets/"),
]
_YEAR_SEG_RE = re.compile(r"/(?:19|20)\d\d(?=/[^/]+$)")


def _direct_file_urls(link: str) -> list[str]:
    """Library-view links (…/Forms/AllItems.aspx?id=<server-relative path>&parent=…)
    name the file outright — candidate direct URLs, most likely first."""
    from urllib.parse import urlsplit, parse_qs, unquote, quote
    u = urlsplit(link)
    # AllItems.aspx (library view), onedrive.aspx, stream.aspx (video player) all carry id=<path>
    if not any(k in u.path.lower() for k in ("allitems.aspx", "onedrive.aspx", "stream.aspx")):
        return []
    path = (parse_qs(u.query).get("id") or [None])[0]
    if not path:
        return []
    path = unquote(path)
    cands = [path]
    for old, new in PATH_REMAPS:
        if old in path:
            moved = path.replace(old, new, 1)
            cands += [moved, _YEAR_SEG_RE.sub("", moved)]
    seen, out = set(), []
    for p in cands:
        if p not in seen:
            seen.add(p)
            out.append(f"{u.scheme}://{u.netloc}" + quote(p))
    return out


NEW_ROOT = "/sites/RAVESoftwareProducts/Shared Documents/Airlines IFE GUI Assets"
_SP_JSON = {"Accept": "application/json;odata=nometadata"}
_FILE_INDEX: dict[str, dict[str, list[str]]] = {}   # airline folder -> basename.lower() -> [paths]


def _sp_api(sess, site: str, path: str, what: str):
    from urllib.parse import quote
    r = sess.get(f"{site}/_api/web/GetFolderByServerRelativePath(decodedurl='{quote(path, safe='')}')/{what}?$select=Name",
                 headers=_SP_JSON, timeout=60)
    return [x["Name"] for x in r.json().get("value", [])] if r.status_code == 200 else []


def _index_tree(sess, site: str, path: str, depth: int = 0, out=None) -> dict:
    """basename.lower() -> [server-relative paths] for every file under `path` (≤6 levels)."""
    out = {} if out is None else out
    for f in _sp_api(sess, site, path, "Files"):
        out.setdefault(f.lower(), []).append(path + "/" + f)
    if depth < 6:
        for d in _sp_api(sess, site, path, "Folders"):
            if d.lower() != "forms":
                _index_tree(sess, site, path + "/" + d, depth + 1, out)
    return out


def _resolve_by_name(sess, moved_path: str) -> str | None:
    """The moved tree was also re-foldered ("First" → "First Class", "B777-300er" →
    "Boeing 777-300ER"), so when the mapped path 404s, find the file by name under
    the airline's folder (then under the whole GUI Assets tree)."""
    from urllib.parse import urlsplit
    if NEW_ROOT not in moved_path:
        return None
    site = "https://zodiacii.sharepoint.com/sites/RAVESoftwareProducts"
    tail = moved_path.split(NEW_ROOT + "/", 1)[1]
    airline, base = tail.split("/", 1)[0], tail.rsplit("/", 1)[-1].lower()
    for scope in (NEW_ROOT + "/" + airline, NEW_ROOT):
        if scope not in _FILE_INDEX:
            print(f"    indexing {scope.split('/')[-1]}…")
            _FILE_INDEX[scope] = _index_tree(sess, site, scope)
        hits = _FILE_INDEX[scope].get(base) or []
        if len(hits) > 1:  # prefer the one sharing the old class folder (Economy/Business/First)
            cls = tail.split("/")[1].lower() if tail.count("/") >= 2 else ""
            pref = [h for h in hits if cls and cls in h.lower()]
            hits = pref or hits
        if hits:
            return hits[0]
    return None


def _fetch_with_cookies(link: str, sess: requests.Session):
    """→ (bytes, ext) via the browser session; None if SharePoint answered with a page instead."""
    from urllib.parse import quote, unquote, urlsplit
    urls = _direct_file_urls(link) or [link + ("&" if "?" in link else "?") + "download=1"]
    moved = next((unquote(urlsplit(u).path) for u in urls if NEW_ROOT in unquote(u)), None)
    if moved:
        found = _resolve_by_name(sess, moved)
        if found:
            urls.append("https://zodiacii.sharepoint.com" + quote(found))
    last = None
    for url in urls:
        r = sess.get(url, timeout=300, allow_redirects=True)
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if "login.microsoftonline" in r.url:
            print("    rejected: sign-in required — cookies expired? re-export them")
            return None
        if r.status_code == 200 and not ctype.startswith("text/html"):
            ext = _EXT_BY_MIME.get(ctype) or Path(url.split("?")[0]).suffix.lower() or mimetypes.guess_extension(ctype) or ".bin"
            return r.content, ext
        last = (r.status_code, ctype or "no type")
    print(f"    rejected ({last[0]}, {last[1]}): file not found at any known path")
    return None


# ── derivatives: originals (iPhone .mov, HEIC, 12 MB photos) stay out of git in
# orig/; the dashboard serves a ≤1600px JPEG or a 720p H.264 MP4 + poster.
ORIG_DIR = MEDIA_DIR / "orig"
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".webm", ".mkv", ".avi"}
MAX_IMG_PX = 1600


def _ffmpeg():
    import shutil
    return shutil.which("ffmpeg")


def _sniff_ext(path: Path) -> str:
    """Fix mislabeled originals (.bin that is really WebP/HEIC/MOV) from magic bytes."""
    head = path.read_bytes()[:16]
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return ".heic" if brand in (b"heic", b"heix", b"mif1", b"msf1") else ".mov"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return path.suffix.lower()


def derive(orig: Path, key: str) -> dict | None:
    """→ {"local": web path, "video": bool, "poster": web path|None} or None on failure."""
    import subprocess
    ext = _sniff_ext(orig)
    web = "/static/uploads/internal/"
    if ext in VIDEO_EXTS:
        mp4, poster = MEDIA_DIR / (key + ".mp4"), MEDIA_DIR / (key + ".poster.jpg")
        ff = _ffmpeg()
        if not ff:
            print("    ffmpeg not found — video kept as original only")
            return None
        if not mp4.exists():
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(orig),
                            "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "96k", str(mp4)],
                           check=True)
        if not poster.exists():
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", "1", "-i", str(mp4), "-frames:v", "1",
                            "-vf", "scale='min(1280,iw)':-2", str(poster)], check=True)
        return {"local": web + mp4.name, "video": True, "poster": web + poster.name}
    jpg = MEDIA_DIR / (key + ".jpg")
    if not jpg.exists():
        try:
            from PIL import Image, ImageOps
            im = Image.open(orig)
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((MAX_IMG_PX, MAX_IMG_PX))
            im.save(jpg, "JPEG", quality=85, optimize=True)
        except Exception as exc:  # noqa: BLE001  (HEIC without pillow-heif, odd formats)
            ff = _ffmpeg()
            if not ff:
                print(f"    cannot convert {ext}: {exc}")
                return None
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(orig), "-frames:v", "1",
                            "-vf", f"scale='min({MAX_IMG_PX},iw)':-2", "-q:v", "3", str(jpg)], check=True)
    return {"local": web + jpg.name, "video": False, "poster": None}


def fetch_one(link: str, auth) -> dict | None:
    """Download a link's file, keep the original under orig/, and return the web
    derivative: {"local": ..., "video": bool, "poster": ...} or None.
    `auth` is a Graph bearer token (str) or a cookie-carrying requests.Session."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    ORIG_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(link.encode("utf-8")).hexdigest()[:14]
    existing = list(ORIG_DIR.glob(key + ".*"))
    if existing:
        return derive(existing[0], key)
    if isinstance(auth, requests.Session):
        got = _fetch_with_cookies(link, auth)
        if not got:
            return None
        orig = ORIG_DIR / (key + got[1])
        orig.write_bytes(got[0])
        return derive(orig, key)
    h = {"Authorization": f"Bearer {auth}"}
    meta = requests.get(f"{GRAPH}/shares/{_share_id(link)}/driveItem", headers=h, timeout=30)
    if meta.status_code != 200:
        print(f"    Graph {meta.status_code}: {meta.text[:120]}")
        return None
    item = meta.json()
    dl = item.get("@microsoft.graph.downloadUrl")
    mime = (item.get("file") or {}).get("mimeType", "")
    ext = _EXT_BY_MIME.get(mime) or Path(item.get("name", "")).suffix.lower() or mimetypes.guess_extension(mime) or ".bin"
    if not dl:
        print("    no download URL (folder or permission issue)")
        return None
    r = requests.get(dl, timeout=300)
    if r.status_code != 200:
        print(f"    download {r.status_code}")
        return None
    orig = ORIG_DIR / (key + ext)
    orig.write_bytes(r.content)
    return derive(orig, key)


def localize_images(images, token, verbose=True) -> int:
    """Mutate image dicts in place: fetch external SharePoint links and mark them local. → count fetched."""
    n = 0
    for im in images or []:
        if not im.get("external") or im.get("local") or not is_sharepoint(im.get("src")):
            continue
        got = fetch_one(im["src"], token)
        if got:
            local = got["local"]
            im["local"] = local
            im["video"] = got["video"]
            if got["poster"]:
                im["poster"] = got["poster"]
            im["external"] = False
            im["origin"] = "sharepoint"
            n += 1
            if verbose:
                print(f"    ✓ {im.get('caption') or im.get('alt') or ''}  → {local}")
    return n


def localize_review(rec, token, verbose=True) -> int:
    n = localize_images(rec.get("images"), token, verbose)
    if n and rec.get("internal_text"):
        rec["internal_text"] = rec["internal_text"].replace(" on SharePoint.", ".")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cookies", default=str(COOKIE_FILE),
                    help="Netscape cookies.txt exported from a browser signed in to SharePoint "
                         f"(default: {COOKIE_FILE.name}); used instead of Graph when present")
    ap.add_argument("--login", action="store_true", help="only sign in to Graph and cache the token")
    ap.add_argument("--login-start", action="store_true",
                    help="issue a device code and exit; a later --login (or the fetch) waits for it")
    a = ap.parse_args()

    if a.login_start:
        app, _ = _msal_app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"device flow failed: {flow.get('error_description') or flow}")
        FLOW_FILE.write_text(json.dumps(flow), encoding="utf-8")
        print(flow["message"])
        return
    if a.login:
        get_token(interactive=True)
        print("Signed in; token cached.")
        return

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    recs = [r for r in data.get("reviews", []) if r.get("media_type") == "internal"
            and any(im.get("external") and not im.get("local") and is_sharepoint(im.get("src"))
                    for im in r.get("images") or [])]
    total = sum(1 for r in recs for im in r["images"] if im.get("external") and not im.get("local"))
    print(f"{len(recs)} internal reviews, {total} SharePoint images to fetch")
    if a.dry_run or not recs:
        return

    token = get_auth(interactive=True, cookies=a.cookies)
    if token is None:
        raise SystemExit("no auth available: export SharePoint cookies to cookies_sharepoint.txt or run --login")
    print("auth: browser cookies" if isinstance(token, requests.Session) else "auth: Microsoft Graph token")
    fetched = 0
    touched = {}

    def save():
        # Merge into the on-disk cache (other jobs write it too); we own images/internal_text.
        fresh = json.loads(CACHE.read_text(encoding="utf-8"))
        for row in fresh.get("reviews", []):
            src = touched.get(row.get("url"))
            if src is not None:
                row["images"] = src.get("images")
                row["internal_text"] = src.get("internal_text")
        CACHE.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")

    for i, r in enumerate(recs, 1):
        print(f"[{i}/{len(recs)}] {r.get('title', '')[:70]}")
        fetched += localize_review(r, token)
        touched[r["url"]] = r
        if i % 5 == 0:
            save()
    save()
    print(f"\nDone: {fetched}/{total} images now served from static/uploads/internal/")


if __name__ == "__main__":
    main()
