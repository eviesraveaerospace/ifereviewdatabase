import re
import json
import time
import requests
import urllib.parse
from pathlib import Path
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    TRANSCRIPTS_AVAILABLE = True
except ImportError:
    TRANSCRIPTS_AVAILABLE = False

# Chapters are parsed from video descriptions at crawl time (same parser the
# daily cloud sweep uses, so tagging is identical).
from gather_chapters import parse_chapters


# ── Source reputation tiers ───────────────────────────────────────────────────
# Tier 1: established aviation / travel trade press
# Tier 2: known aviation YouTube channels / specialist blogs
# Tier 3: general / unknown

SOURCE_TIERS = {
    # Tier 1 — aviation & travel trade press
    1: [
        "simpleflying.com", "airlinegeeks.com", "aviationweek.com",
        "thepointsguy.com", "onemileatatime.com", "boardingarea.com",
        "airlinereporter.com", "skift.com", "flightglobal.com",
        "routesonline.com", "ch-aviation.com", "atwonline.com",
        "aerotime.aero", "aviationbusinessnews.com", "passengerexperience.aero",
        "apex.aero", "aircraft-interior-expo.com",
        "paxinternational.com", "businesstraveller.com", "airlineratings.com",
        "ainonline.com", "aircraft-interiors-international.com", "runwaygirlnetwork.com",
        "aviationpros.com", "cntraveler.com", "travelandleisure.com",
        "paxex.aero", "aircraftinteriorsinternational.com",
    ],
    # Tier 2 — specialist aviation/travel creators and blogs
    2: [
        "youtube.com", "samchui.com", "noelphilips.com",
        "flyertalk.com", "airfarewatchdog.com", "headsforaplane.com",
        "travelisfree.com", "ausbt.com.au", "executive-traveller.com",
        "headforpoints.com", "loungebuddy.com", "thedesignair.net",
        "seatguru.com", "airlinequality.com", "joshcahill.com",
    ],
}

def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""

def source_tier(url: str) -> int:
    d = _domain(url)
    for tier, domains in SOURCE_TIERS.items():
        if any(d == dom or d.endswith("."+dom) for dom in domains):
            return tier
    return 3

TIER_LABELS = {1: "Press", 2: "Creator", 3: "General"}

# Detects titles that read as official airline/brand promotional content rather
# than independent creator reviews. High-precision — only very explicit signals.
_OFFICIAL_PROMO_RE = re.compile(
    r'^\s*introducing\b'                                      # "Introducing ICE:..."
    r'|^\s*discover\s+(?:our|the)\b'                          # "Discover our new..."
    r'|^\s*welcome\s+(?:aboard|to\s+our)\b'                   # "Welcome aboard..."
    r'|\bpresents?\s+(?:its\s+|the\s+|our\s+|new\s+)*(?:ife|inflight|in.flight|entertainment|system)\b'
    r'|\bour\s+new\s+(?:ife|inflight|in.flight|entertainment)\b'
    r'|\bnew\s+.*\bentertainment\s+system\b.*\blaunch\b'
    r'|^\s*(?:unveiling|launching|announcing)\b',
    re.IGNORECASE,
)

_OFFICIAL_AIRLINE_CHANNELS = {"emirates", "singapore airlines", "etihad airways", "qatar airways"}

def is_official_promo(title: str, channel_title: str = "") -> bool:
    if channel_title and channel_title.strip().lower() in _OFFICIAL_AIRLINE_CHANNELS:
        return True
    return bool(_OFFICIAL_PROMO_RE.search(title))


# ── IFE keyword gate ──────────────────────────────────────────────────────────
# Hotel/resort/land-lodging titles with zero aviation context are not IFE content
_HOTEL_TITLE_RE = re.compile(r'\bhotels?\b|\bresorts?\b|\bairbnb\b|\bvillas?\b|\bhostels?\b', re.I)
_AVIATION_CONTEXT_RE = re.compile(
    r'flight|airline|airways|air lines|business class|first class|economy|premium economy'
    r'|boeing|airbus|\ba3\d{2}\b|\b7\d7\b|dreamliner|lounge|airport|\bife\b|inflight|in-flight',
    re.I,
)


# Documentary / explainer titles ("Why Aviation Safety Is Better Than You
# Think", "1 in 5.6 Million", "Why planes crash") name aviation but are not
# flight, cabin, or IFE reviews. High-precision phrases only — never a bare
# "safety" or "aviation".
_EXPLAINER_RE = re.compile(
    r'\baviation\s+safety\b'
    r'|\b1\s+in\s+\d[\d,.]*\s*(?:million|billion)\b'
    r'|\bwhy\b.*\b(?:is|are)\s+(?:better|safer|worse)\s+than\s+you\s+think\b'
    r"|\b(?:why|how)\s+(?:do\s+)?(?:planes|airplanes|aircraft|jets)\s+(?:crash|fly|stay|don'?t|never)\b"
    r'|\bplane\s+crash(?:es)?\b'
    r'|\bair\s+disasters?\b'
    r'|\bmayday\b'
    r'|\bblack\s+box\b'
    r'|\brise\s+and\s+fall\s+of\b'
    r'|\b(?:history|story)\s+of\s+(?:the\s+)?(?:boeing|airbus|a3\d{2}|7\d7)\b',
    re.IGNORECASE,
)

# Channels whose output is never IFE/airline-review content (lowercase, exact).
_BLOCKED_CHANNELS = {"the strange file"}


def _iso_duration_seconds(duration_iso: str) -> Optional[int]:
    """ISO 8601 duration (PT1H2M3S) → seconds; None if unparseable/empty."""
    if not duration_iso:
        return None
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
    if not m:
        return None
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


# YouTube Shorts run up to 3 minutes; anything at or under that from a Shorts
# URL/hashtag or duration is displayed vertically in the dashboard.
SHORT_MAX_SECONDS = 180

_SHORTS_TAG_RE = re.compile(r'#shorts?\b|\|\s*shorts\b', re.IGNORECASE)


def _is_short(title: str, duration_seconds: Optional[int] = None, url: str = "") -> bool:
    if "/shorts/" in (url or ""):
        return True
    if _SHORTS_TAG_RE.search(title or ""):
        return True
    return duration_seconds is not None and 0 < duration_seconds <= SHORT_MAX_SECONDS


_REVIEW_SIGNAL_RE = re.compile(
    r'\b(review|reviews|reviewed|class|flight|flying|flew|trip report|cabin|seat|seats'
    r'|ife|entertainment|onboard|on board|inflight|in-flight)\b',
    re.IGNORECASE,
)


def _is_airline_review_title(title: str) -> bool:
    """A title that names a known airline AND reads like a review/flight report
    ("My Iceland Air review #shorts") — lets genuine airline Shorts through."""
    return bool(_keyword_hits(title, AIRLINE_KEYWORDS)) and bool(_REVIEW_SIGNAL_RE.search(title))


def _is_spam_video(title: str, duration_iso: str = "", channel_title: str = "") -> bool:
    """Return True for viral spam, hotel/resort junk, hashtag floods, aviation
    explainers/documentaries, blocked channels, and Shorts that are NOT airline
    reviews (airline-review Shorts are kept and shown vertically)."""
    if channel_title and channel_title.strip().lower() in _BLOCKED_CHANNELS:
        return True
    if _EXPLAINER_RE.search(title):
        return True
    hashtags = re.findall(r'#\w+', title)
    if len(hashtags) >= 4:
        return True
    if _HOTEL_TITLE_RE.search(title) and not _AVIATION_CONTEXT_RE.search(title):
        return True
    secs = _iso_duration_seconds(duration_iso)
    tagged_short = bool(_SHORTS_TAG_RE.search(title))
    too_short = secs is not None and secs < 90
    if (tagged_short or too_short) and not _is_airline_review_title(title):
        return True
    return False


# Broad keywords that are only valid when they appear in the TITLE — not descriptions.
# Descriptions of AI drama, movie reviews, etc. also contain these words.
_IFE_TITLE_ONLY_KEYWORDS = {
    "business class review", "first class review", "economy class review",
    "premium economy review", "cabin review", "seat review", "flight review",
    "entertainment screen", "video on demand",
    "inflight wifi review", "airline wifi review", "starlink wifi flight",
}

# Standalone "ife" word — needs boundary check to avoid matching "life", "wife", "knife"
_IFE_WORD_RE = re.compile(r'\bife\b', re.IGNORECASE)


IFE_TITLE_KEYWORDS = [
    "inflight entertainment", "in-flight entertainment", "in flight entertainment",
    "ife system", "ife review", "ifec", "astrova",
    "panasonic ex3", "panasonic ex2", "panasonic ex1", "panasonic astrova",
    "thales avant", "thales inflyt", "safran rave", "spi rave", "rave aerospace",
    "emirates ice", "viasat ife", "oryx one", "krisworld",
    "studiocx", "studioex", "planet ife", "collins venue",
    "seatback entertainment", "seatback screen", "seatback display", "seatback",
    "airline entertainment review", "flight entertainment system",
    "4k ife", "4k inflight", "oled inflight",
    # broader flight-review terms — nearly every cabin/seat review covers IFE
    "business class review", "first class review", "economy class review",
    "premium economy review", "cabin review", "seat review", "flight review",
    "entertainment screen", "video on demand", "seatback screen",
    "inflight wifi review", "airline wifi review", "starlink wifi flight",
    "gogo wifi", "ife award", "passenger choice award",
    "gogo avance", "anuvu", "immfly", "bluebox",
    # French — Air France, Corsair, Transavia
    "divertissement à bord", "divertissement en vol", "système de divertissement",
    "écran siège", "écran de siège", "ife avis", "divertissement bord",
    "avis vol", "revue vol", "test vol", "classe affaires avis",
    # German — Lufthansa, Swiss, Austrian
    "bordunterhaltung", "unterhaltungssystem", "sitzbildschirm",
    "inflight entertainment test", "inflight entertainment bewertung",
    "business class bewertung", "erfahrungsbericht flug",
    # Japanese — ANA, JAL
    "機内エンターテインメント", "機内エンタメ", "シートモニター",
    "ビジネスクラス レビュー", "エコノミー レビュー", "機内 レビュー",
    # Korean — Korean Air, Asiana
    "기내 엔터테인먼트", "기내 오락", "좌석 모니터", "기내 리뷰",
    "비즈니스 클래스 리뷰", "대한항공 리뷰", "아시아나 리뷰",
    # Chinese — China Airlines, Air China, China Eastern, EVA Air
    "机内娱乐", "机舱娱乐", "座椅屏幕", "影音系统",
    "機內娛樂", "座椅螢幕", "商務艙評測", "头等舱评测",
    # Spanish — Iberia, LATAM, Avianca
    "entretenimiento a bordo", "pantalla del asiento", "sistema de entretenimiento",
    "clase ejecutiva opinión", "clase turista revisión", "reseña vuelo",
    # Portuguese — TAP, LATAM Brasil
    "entretenimento a bordo", "tela do assento", "sistema de entretenimento",
    "avaliação voo", "classe executiva avaliação", "revisão voo",
    # Turkish — Turkish Airlines
    "uçuş eğlence sistemi", "koltuk ekranı", "iş sınıfı inceleme",
    # Italian — ITA Airways, Neos
    "intrattenimento a bordo", "schermo del sedile", "business class recensione",
]


# ── IFE system detection ──────────────────────────────────────────────────────
IFE_SYSTEM_PATTERNS = {
    "Emirates ICE":        ["emirates ice", "information, communication, entertainment"],
    "Panasonic Astrova":   ["panasonic astrova", "astrova 4k", "astrova oled"],
    "Panasonic eX3":       ["panasonic ex3", "ex3 ife", "panasonic avionics ex3"],
    "Panasonic eX2":       ["panasonic ex2"],
    "Panasonic eX1":       ["panasonic ex1"],
    "Thales AVANT Up":     ["avant up", "thales avant up"],
    "Thales AVANT":        ["thales avant", "thales inflyt", "inflyt experience"],
    "Safran RAVE Ultra":   ["rave ultra", "safran rave ultra", "spi rave ultra"],
    "Safran RAVE":         ["safran rave", " rave ife", "safran passenger innovations",
                            " spi ife", "spi inflight", "spi entertainment", "rave aerospace"],
    "Collins Venue":       ["collins venue", "rockwell collins venue"],
    "Viasat (streaming)":  ["viasat"],
    "Inmarsat GX":         ["inmarsat gx", "gx aviation"],
    "Oryx One":            ["oryx one"],
    "KrisWorld":           ["krisworld"],
    "StudioCX":            ["studiocx", "studio cx"],
    "Lumexis FTTS":        ["lumexis", "ftts"],
    "Gogo Avance":         ["gogo avance", "gogo vision", "gogo inflight"],
    "Anuvu":               ["anuvu", "global eagle entertainment"],
    "Immfly":              ["immfly"],
    "Bluebox Wow":         ["bluebox wow", "bluebox aviation"],
}


# ── IFE feature detection ─────────────────────────────────────────────────────
IFE_FEATURE_KEYWORDS = {
    "entertainment_system": ["entertainment system", "ife", "in-flight entertainment", "seatback screen", "vod",
                             "video on demand"],
    "content":              ["movies", "tv shows", "tv series", "music", "games", "podcasts", "content library"],
    "connectivity":         ["wifi", "wi-fi", "internet", "connectivity", "bluetooth", "starlink", "onair",
                             "inflight connectivity"],
    "4k_display":           ["4k", "4k display", "4k screen", "uhd", "oled", "amoled", "4k oled", "mini-led screen"],
    "quality":              ["resolution", "display", "picture quality", "1080p", "touchscreen", "hd screen"],
    "seat":                 ["seat", "recline", "legroom", "comfort", "headrest"],
    "usb_power":            ["usb", "usb-c", "charging", "power outlet", "ac outlet"],
    "bluetooth_audio":      ["bluetooth", "wireless headphones", "airpods", "bluetooth audio"],
    # In-IFE product features (what the system itself can do)
    "watch_party":          ["watch party", "watch together", "group watch", "shared viewing"],
    "seat_chat":            ["seat-to-seat chat", "seat to seat chat", "seat-to-seat messaging",
                             "seat to seat messaging", "seat chat", "message other passengers",
                             "chat with other passengers"],
    "search":               ["search bar", "search function", "search feature", "search option", "search menu"],
    "tail_camera":          ["tail camera", "tail cam", "external camera", "exterior camera", "outside camera",
                             "nose camera", "belly camera", "downward camera", "camera view", "external view",
                             "outside view", "cameras on the plane"],
    "moving_map":           ["moving map", "flight map", "map view", "flight tracker", "3d map", "flight path",
                             "flightpath", "interactive map", "flight progress"],
}

_ARTICLE_EXCERPT_KEYWORDS = {kw for kws in IFE_FEATURE_KEYWORDS.values() for kw in kws}
_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"‘“])')


def _article_excerpt(raw_text: str) -> Optional[str]:
    """Pick the most IFE-relevant sentence from a full article body to use as
    a display excerpt (mirrors how video transcript excerpts are chosen)."""
    clean = re.sub(r'\s+', ' ', raw_text).strip()
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(clean)]
    candidates = [s for s in sentences if 40 <= len(s) <= 280]
    if not candidates:
        return None

    best, best_score = None, -1
    for s in candidates:
        sl = s.lower()
        score = sum(1 for kw in _ARTICLE_EXCERPT_KEYWORDS if kw in sl)
        if score > best_score:
            best, best_score = s, score
    return best if best_score > 0 else candidates[0]


def _article_quotes(raw_text: str, limit: int = 8):
    """Top IFE-relevant sentences from an article body, in reading order, each
    with the feature keywords that matched — shown in the article modal."""
    clean = re.sub(r'\s+', ' ', raw_text).strip()
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(clean)]
    scored = []
    for pos, s in enumerate(sentences):
        if not (40 <= len(s) <= 300):
            continue
        sl = s.lower()
        kws = [kw for kw in _ARTICLE_EXCERPT_KEYWORDS if kw in sl]
        if kws:
            scored.append((len(kws), pos, s, kws))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = sorted(scored[:limit], key=lambda x: x[1])
    return [{"text": s, "kws": sorted(kws, key=len, reverse=True)[:3]}
            for _, pos, s, kws in top]


_ARTICLE_BOILERPLATE_RE = re.compile(
    r'^(share this|subscribe|sign up|read more|advertisement|related articles?|'
    r'cookie|©|copyright|all rights reserved|follow us)', re.IGNORECASE)


def _article_paragraphs(soup, max_paragraphs: int = 80, max_chars: int = 25000) -> List[str]:
    """Readable body paragraphs of a press article, in order, for inline display.
    Prefers the <article> element; skips nav/footer/script and short fragments."""
    root = soup.find("article") or soup.find("main") or soup.body or soup
    for tag in root.find_all(["script", "style", "nav", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    out, total = [], 0
    for el in root.find_all(["p", "h2", "h3", "li", "blockquote"]):
        txt = re.sub(r'\s+', ' ', el.get_text(" ")).strip()
        if len(txt) < 40 and el.name not in ("h2", "h3"):
            continue
        if not txt or _ARTICLE_BOILERPLATE_RE.search(txt):
            continue
        if out and out[-1] == txt:
            continue
        out.append(txt)
        total += len(txt)
        if len(out) >= max_paragraphs or total >= max_chars:
            break
    return out


# ── Airlines & aircraft ───────────────────────────────────────────────────────
AIRLINE_KEYWORDS = [
    "emirates", "qatar airways", "etihad", "lufthansa", "british airways",
    "singapore airlines", "cathay pacific", "ana", "japan airlines", "jal",
    "air france", "klm", "turkish airlines", "finnair", "air canada",
    "united airlines", "american airlines", "delta", "southwest", "alaska airlines",
    "air india", "china airlines", "korean air", "eva air", "thai airways",
    "virgin atlantic", "iberia", "tap air portugal", "swiss", "austrian airlines",
    "qantas", "air new zealand", "china eastern", "china southern", "hainan airlines",
    "level", "wizz air", "ryanair", "easyjet",
    # Safran RAVE operators (must stay in sync with AIRLINE_IFE_LOOKUP)
    "icelandair", "sun country", "volaris", "frontier airlines", "allegiant",
    # other frequently reviewed carriers
    "jetblue", "hawaiian airlines", "aer lingus", "ita airways", "westjet",
    "latam", "avianca", "condor", "discover airlines", "breeze airways",
    "porter airlines", "spirit airlines", "zipair", "saudia", "aeromexico",
]

AIRCRAFT_KEYWORDS = [
    "787", "777", "737", "a350", "a380", "a330", "a321", "a320", "a220",
    "dreamliner", "airbus", "boeing", "737 max", "a321neo", "a350-900", "a350-1000",
    "777x", "787-9", "787-10",
]


# Word-boundary keyword matching — short names like "ana", "jal", "level",
# "swiss", or "delta" must not match inside unrelated words ("banana", "wife",
# "sea level"). Substring `kw in text` matching is what let story-narration and
# anime spam through the relevance gate.
_KEYWORD_RE_CACHE: Dict[str, "re.Pattern"] = {}

# Alternate spellings people use for a carrier. Matching any alias counts as a
# hit for the canonical keyword, so tags, filters, search and the crawl gate all
# see "Iceland Air" / "Iceland-Air" as Icelandair.
AIRLINE_ALIASES: Dict[str, List[str]] = {
    "icelandair": ["iceland air", "iceland-air", "icelandic air"],
}


def _keyword_re(kw: str) -> "re.Pattern":
    pat = _KEYWORD_RE_CACHE.get(kw)
    if pat is None:
        variants = [kw] + AIRLINE_ALIASES.get(kw.lower(), [])
        body = "|".join(re.escape(v) for v in variants)
        pat = re.compile(r'(?<![\w-])(?:' + body + r')(?![\w-])', re.IGNORECASE)
        _KEYWORD_RE_CACHE[kw] = pat
    return pat

def _keyword_hits(text: str, keywords: List[str]) -> List[str]:
    return [kw for kw in keywords if _keyword_re(kw).search(text)]


# ── Structured spec extraction ────────────────────────────────────────────────
SPEC_PATTERNS = {
    "screen_size": [
        r'(\d{1,2}(?:\.\d)?)[- ]?(?:inch|in\b|\")\s*(?:screen|display|monitor|touch)',
        r'(?:screen|display|monitor)\s+(?:is\s+)?(\d{1,2}(?:\.\d)?)[- ]?(?:inch|in\b|\")',
    ],
    "content_count": [
        r'(\d[\d,]+)\+?\s*(?:titles|movies|channels|hours? of content|content options|video options)',
        r'over\s+(\d[\d,]+)\s*(?:titles|movies|channels)',
        r'more than\s+(\d[\d,]+)\s*(?:titles|movies|channels)',
    ],
    "wifi_type": {
        "Starlink": ["starlink"],
        "Ka-band":  ["ka-band", "ka band", "inmarsat gx", "viasat ka"],
        "Ku-band":  ["ku-band", "ku band"],
        "Streaming":["streaming ife", "stream from your", "bring your own device", "byod"],
        "Wi-Fi":    ["wi-fi", "wifi", "on-board wifi"],
    },
    "controller": {
        "Touchscreen":       ["touchscreen", "touch screen", "touch-screen"],
        "Handheld remote":   ["handheld", "remote control", "handset"],
        "Trackpad":          ["trackpad", "track pad"],
        "Tablet":            ["tablet", "ipad"],
    },
}

def _extract_specs(text: str) -> dict:
    specs = {}
    t = text.lower()

    # Screen size — take the largest plausible value found
    sizes = []
    for pat in SPEC_PATTERNS["screen_size"]:
        for m in re.finditer(pat, t):
            try:
                v = float(m.group(1))
                if 6 <= v <= 32:          # sane IFE screen range
                    sizes.append(v)
            except ValueError:
                pass
    if sizes:
        specs["screen_size"] = f'{max(sizes):.0f}"'

    # Content count — take largest number found
    counts = []
    for pat in SPEC_PATTERNS["content_count"]:
        for m in re.finditer(pat, t):
            try:
                counts.append(int(m.group(1).replace(",", "")))
            except ValueError:
                pass
    if counts:
        c = max(counts)
        specs["content_count"] = f"{c:,}+"

    # WiFi type
    for label, keywords in SPEC_PATTERNS["wifi_type"].items():
        if any(kw in t for kw in keywords):
            specs["wifi_type"] = label
            break

    # Controller type
    for label, keywords in SPEC_PATTERNS["controller"].items():
        if any(kw in t for kw in keywords):
            specs["controller"] = label
            break

    return specs


# ── IFE system inference (airline + aircraft fallback) ───────────────────────
# Used when text-based detection finds nothing. Prefer airline-specific
# mappings; fall back to aircraft-type defaults.

AIRLINE_IFE_LOOKUP = {
    "emirates":          "Emirates ICE",
    "qatar airways":     "Oryx One",
    "singapore airlines":"KrisWorld",
    "cathay pacific":    "StudioCX",
    "ana":               "Panasonic eX3",
    "japan airlines":    "Panasonic eX3",
    "jal":               "Panasonic eX3",
    "lufthansa":         "Thales AVANT",
    "british airways":   "Panasonic eX3",
    "turkish airlines":  "Panasonic eX3",
    "finnair":           "Panasonic eX3",
    "air france":        "Thales AVANT",
    "klm":               "Thales AVANT",
    "etihad":            "Panasonic eX3",
    "air canada":        "Panasonic eX3",
    "united airlines":   "Panasonic eX3",
    "american airlines": "Panasonic eX3",
    "delta":             "Thales AVANT",
    "alaska airlines":   "Viasat (streaming)",
    "southwest":         "Viasat (streaming)",
    "china airlines":    "Panasonic eX3",
    "korean air":        "Panasonic eX3",
    "eva air":           "Panasonic eX3",
    "thai airways":      "Thales AVANT",
    "virgin atlantic":   "Thales AVANT",
    "iberia":            "Thales AVANT",
    "swiss":             "Thales AVANT",
    "austrian airlines": "Panasonic eX3",
    "qantas":            "Panasonic eX3",
    "air new zealand":   "Panasonic eX3",
    "china eastern":     "Thales AVANT",
    "china southern":    "Panasonic eX3",
    "hainan airlines":   "Thales AVANT",
    "air india":         "Panasonic eX3",
    "tap air portugal":  "Thales AVANT",
    # Safran RAVE operators
    "icelandair":        "Safran RAVE",
    "sun country":       "Safran RAVE",
    "volaris":           "Safran RAVE",
    "frontier":          "Safran RAVE",
    "frontier airlines": "Safran RAVE",
    "allegiant":         "Safran RAVE",
}

AIRCRAFT_IFE_DEFAULTS = {
    "a380":    "Panasonic eX3",
    "a350":    "Panasonic eX3",
    "a330":    "Panasonic eX3",
    "a321neo": "Viasat (streaming)",
    "a321":    "Viasat (streaming)",
    "a320":    "Viasat (streaming)",
    "a220":    "Panasonic eX3",
    "787-10":  "Panasonic eX3",
    "787-9":   "Panasonic eX3",
    "787":     "Panasonic eX3",
    "777x":    "Panasonic Astrova",
    "777":     "Panasonic eX3",
    "737 max": "Viasat (streaming)",
    "737":     "Viasat (streaming)",
}


def infer_ife_system(airlines: list, aircraft: list) -> Optional[str]:
    """Return best-guess IFE system from airline/aircraft when text detection fails."""
    # 1. Airline lookup (most reliable — carriers rarely mix systems fleet-wide)
    for a in airlines:
        kw = a.get("keyword", "").lower()
        if kw in AIRLINE_IFE_LOOKUP:
            return AIRLINE_IFE_LOOKUP[kw]
    # 2. Aircraft fallback (longer keys first so 737 max matches before 737)
    ac_keywords = [a.get("keyword", "").lower() for a in aircraft]
    for key in sorted(AIRCRAFT_IFE_DEFAULTS, key=len, reverse=True):
        if any(key in ac for ac in ac_keywords):
            return AIRCRAFT_IFE_DEFAULTS[key]
    return None


# ── Known IFE reviewer YouTube channels ──────────────────────────────────────
# How to find a channel ID:
#   1. Go to the channel's YouTube page
#   2. Right-click → View Page Source
#   3. Ctrl+F → search for "channelId" — copy the 24-character UC... value
# Each channel search costs 100 API units, same as a keyword search.
KNOWN_IFE_CHANNELS: dict = {
    # Verified channel IDs — each costs 100 API units/day
    # To verify: channels?part=snippet&id=UC... (1 unit, batch up to 50)
    "Million Miles Marc":       "UCGZI_9g_4mWWZbTvO1N9Y4Q",  # verified ✓
    "Chris Films Things":       "UCIIotzUXweA445T6h8fBXRQ",  # verified ✓
    "theplanesguy":             "UClm9qlyx-E68Q3gGuaBj-SQ",  # verified ✓
    "From the Wing":            "UCOBUoOstpv-yCHZk2B77gXw",  # verified ✓
    "Nonstop Dan":              "UCrLe85KbtkqnnSnIVc-KLjA",  # main flight-review channel (old ID was Nonstop Dan Vlogs — hotels)
    "Simply Aviation":          "UCEF-9XhkdyFY0hMRUkmxXfQ",  # verified ✓
    "Eric Struk":               "UCDv-Fv9bAt-1bU9EBOnqHvw",  # verified ✓
    "Dennis Bunnik":            "UCQrk97MBH6DToctKRfQJcNQ",  # verified ✓ (DennisBunnik Travels)
    "iTripReport":              "UCujsRp13yioFAhhHZcEgkYw",  # verified ✓
    "Jayden Wong":              "UChH-Hz5BxUKDA0olzqcAGpw",  # verified ✓
    "RoryDing Travels":         "UClLxsJJL11SLXwh4C8I3sWQ",  # verified ✓
    "First Travel":             "UCQRRH7H30Z_kSKW4TsPuKew",  # verified ✓
    "Sam Chui":                 "UCfYCRj25JJQ41JGPqiqXmJw",  # verified ✓
    "Luxury Travel Expert":     "UCYxsXxbjJO1YYa9yQ3lKC8w",  # verified ✓
    "TPG Travels":              "UCufeRIBzaIc2MAJbbZhPJEg",  # verified ✓ (The Points Guy)
    "Flight Formula":           "UCFCtPTN9M6nmmDbllaZgGuA",  # verified ✓
    "The Window Seat":          "UCuUfpJI98M_s-zYlLmAScdA",  # verified ✓ (API-resolved)
    "Flights And Frustration":  "UCnEaBTNYsS7NKIm3W4Kq63A",  # verified ✓ (API-resolved)
    "Josh Cahill":              "UCJmkopWuQxI9U0NQaOcl68Q",  # verified ✓ (API-resolved)
    "Project Travel":           "UC5oH7KZ2b1yeXr52Q_4gPlg",  # verified ✓ (API-resolved)
    "Ethan G":                  "UCw5zdNpiAV-o6gtu4cOnb9A",  # verified ✓ (API-resolved)
    "Nonstop Eurotrip":         "UCBqWe9KKYUavknEaaZLk7cQ",  # verified ✓ (API-resolved)
    "Travel Tips by Laurie":    "UCEZKpVw6ldXNVU4Ua6IFwTw",  # verified ✓ (API-resolved)
    "Trip Reviews":             "UC4j-y5vuChsMZl4-YsbRWvw",  # verified ✓ (API-resolved)
    "Luxury Travel Diary":      "UCyvOVVytophF3HZNFlaK1ZQ",  # verified ✓ (API-resolved)
    "AirlineReporter":          "UCkiY21AyoawQdK6mzhidtnw",  # verified ✓ (API-resolved)

    # Official airline channels — for genuine "Official" promo/showcase content,
    # as distinct from the independent reviewer channels above.
    "Emirates (official)":          "UCJ6jdm9qTla9Lp3Jf-TPwbg",  # verified ✓ 1.9M subs
    "Singapore Airlines (official)":"UCIrr4E2y6Cv-2FSxk1_PBGQ",  # verified ✓ 133K subs
    "Etihad Airways (official)":    "UCVl7yuQhcmRpv3iydZ97eUw",  # verified ✓ 349K subs
    "Qatar Airways (official)":     "UCi8xUU_lg3zr8UcBXLdEheQ",  # verified ✓
}


# ── Search queries ────────────────────────────────────────────────────────────
AUTO_DISCOVERY_QUERIES = [
    '"inflight entertainment" review 2024',
    '"inflight entertainment" review 2025',
    '"inflight entertainment" review 2026',
    '"in-flight entertainment" IFE system review 2025',
    '"Panasonic Astrova" airline review',
    '"Panasonic eX3" airline review',
    '"Thales AVANT" airline review',
    '"Thales AVANT Up" airline inflight entertainment',
    '"Safran RAVE" inflight entertainment',
    '"Safran RAVE Ultra" IFE review',
    '"Safran Passenger Innovations" IFE',
    '"SPI RAVE" inflight entertainment',
    '"RAVE Aerospace" inflight entertainment',
    '"Emirates ICE" inflight entertainment',
    '"Oryx One" Qatar inflight entertainment',
    '"KrisWorld" Singapore Airlines entertainment',
    'airline IFE system review 2024',
    'airline IFE system review 2025',
    'airline IFE system review 2026',
    'seatback entertainment system airline review 2025',
    '"4K inflight entertainment" review',
    '"Panasonic Astrova" 4K OLED IFE',
    'Starlink inflight wifi airline 2024',
    'Starlink inflight wifi airline 2025',
    '"best inflight entertainment" airline award 2024',
    '"best inflight entertainment" airline award 2025',
    'APEX passenger choice award inflight entertainment 2024',
    'APEX passenger choice award inflight entertainment 2025',
    '"Gogo Avance" airline inflight entertainment',
    '"Anuvu" inflight entertainment airline',
    'new inflight entertainment system airline launch 2024',
    'new inflight entertainment system airline launch 2025',
    'aircraft interiors expo IFE system 2024',
    'airline wifi Starlink passenger review 2025',
    # Site-targeted — PaxEx trade press (Runway Girl Network and peers)
    'site:runwaygirlnetwork.com inflight entertainment',
    'site:runwaygirlnetwork.com IFE review',
    'site:apex.aero inflight entertainment',
    'site:paxex.aero inflight entertainment',
    'site:aircraftinteriorsinternational.com inflight entertainment',
    # French (Air France, Corsair — both RAVE Ultra operators)
    '"divertissement à bord" Air France avis',
    '"divertissement en vol" Air France classe affaires',
    'Corsair "divertissement bord" avis',
    # German (Lufthansa, Swiss, Austrian)
    '"Bordunterhaltung" Lufthansa Test Bewertung',
    'Lufthansa Business Class "inflight entertainment" Bewertung',
    # Japanese (ANA, JAL — Panasonic major customer)
    'ANA 機内エンターテインメント レビュー',
    'JAL 機内エンターテインメント レビュー',
    # Korean (Korean Air — RAVE operator)
    '대한항공 기내 엔터테인먼트 리뷰',
    # Chinese (China Airlines, Air China, EVA Air)
    '中华航空 机内娱乐系统 评测',
    '长荣航空 机内娱乐 评测',
    # Spanish (Iberia, LATAM — Panasonic/Thales)
    'Iberia "entretenimiento a bordo" opinión clase',
    'LATAM "entretenimiento a bordo" revisión',
    # Portuguese (TAP Air Portugal — Thales)
    'TAP "entretenimento a bordo" avaliação',
    # Turkish (Turkish Airlines — Panasonic)
    'Türk Hava Yolları uçuş inceleme eğlence sistemi',
]

YOUTUBE_QUERIES = [
    # ── IFE systems ───────────────────────────────────────────────────────────
    # (system name is the differentiator — no year needed, date handled by published_after)
    "Panasonic Astrova inflight entertainment review",
    "Panasonic eX3 inflight entertainment review",
    "Thales AVANT inflight entertainment review",
    "Thales AVANT Up inflight entertainment review",
    "Emirates ICE inflight entertainment review",
    "Safran RAVE Ultra inflight entertainment review",
    "SPI RAVE inflight entertainment review",
    "RAVE Aerospace inflight entertainment review",
    "Safran Passenger Innovations inflight entertainment review",
    "Oryx One inflight entertainment review",
    "KrisWorld inflight entertainment review",
    "StudioCX Cathay Pacific entertainment review",
    "Collins Venue IFE review",
    "Gogo Avance inflight wifi review",
    "Viasat inflight wifi airline review",

    # ── Airlines ─────────────────────────────────────────────────────────────
    # One query per airline covers all cabin classes — no need to repeat per class
    "Emirates inflight entertainment review",
    "Qatar Airways inflight entertainment review",
    "Singapore Airlines inflight entertainment review",
    "Cathay Pacific inflight entertainment review",
    "ANA inflight entertainment review",
    "Japan Airlines inflight entertainment review",
    "Lufthansa inflight entertainment review",
    "British Airways inflight entertainment review",
    "Turkish Airlines inflight entertainment review",
    "Air France inflight entertainment review",
    "Virgin Atlantic RAVE Ultra inflight entertainment",
    "Finnair inflight entertainment review",
    "Delta inflight entertainment review",
    "United Airlines inflight entertainment review",
    "American Airlines inflight entertainment review",
    "Etihad inflight entertainment review",
    "Korean Air inflight entertainment review",
    "Qantas inflight entertainment review",
    "Icelandair RAVE inflight entertainment",
    "Icelandair review",
    "Icelandair Saga class review",
    "Icelandair economy class review",
    "Iceland Air review",
    "Iceland Air flight review",
    "Icelandair review #shorts",
    "Air India inflight entertainment review",
    "Oman Air inflight entertainment review",
    "STARLUX Airlines inflight entertainment review",
    "STARLUX Airlines economy class review",
    "STARLUX Airlines business class review",
    "China Airlines inflight entertainment review",
    "EVA Air inflight entertainment review",
    "Saudia inflight entertainment review",
    "Vietnam Airlines inflight entertainment review",
    "Hawaiian Airlines inflight entertainment review",
    "JetBlue inflight entertainment review",

    # ── WiFi & Starlink ───────────────────────────────────────────────────────
    "Alaska Airlines Starlink wifi review",
    "Delta Starlink inflight wifi review",
    "Air New Zealand Starlink inflight wifi review",
    "airline inflight wifi speed test",

    # ── Aircraft ──────────────────────────────────────────────────────────────
    "A380 inflight entertainment review",
    "A350 inflight entertainment review",
    "A330neo inflight entertainment review",
    "A330-900 inflight entertainment review",
    "Boeing 787 inflight entertainment review",
    "Boeing 777 inflight entertainment review",
    "A321neo inflight entertainment review",

    # ── Industry & awards ─────────────────────────────────────────────────────
    "best airline inflight entertainment award",
    "APEX passenger choice award inflight entertainment",
    "new inflight entertainment system launch",

    # ── General ───────────────────────────────────────────────────────────────
    "inflight entertainment IFE review",
    "4K inflight entertainment seatback review",
    "OLED inflight entertainment review",

    # ── Non-English ───────────────────────────────────────────────────────────
    "Air France divertissement bord avis",
    "Lufthansa Bordunterhaltung Bewertung",
    "ANA 機内エンターテインメント レビュー",
    "JAL 機内エンターテインメント レビュー",
    "대한항공 기내 엔터테인먼트 리뷰",
    "아시아나항공 기내 엔터테인먼트 리뷰",
    "中华航空 机内娱乐 评测",
    "长荣航空 机内娱乐 评测",
    "星宇航空 機上娛樂 評測",
    "STARLUX 星宇航空 經濟艙 開箱",
    "Iberia entretenimiento a bordo reseña",
    "LATAM entretenimiento a bordo reseña",
    "TAP Air Portugal entretenimento bordo avaliação",
    "Turkish Airlines inflight entertainment inceleme",
]

# ── Systematic discovery ──────────────────────────────────────────────────────
# A broad airline list so coverage isn't limited to the hand-picked queries above.
# Each airline is expanded into a few review-intent queries (below). The
# published_after date filter keeps results recent, and the aviation-review gate
# drops non-IFE noise, so a wide net here mostly surfaces genuine reviews.
GLOBAL_AIRLINES = [
    # Middle East
    "Emirates", "Qatar Airways", "Etihad", "Saudia", "Oman Air", "Gulf Air",
    "Kuwait Airways", "Royal Jordanian", "flydubai", "Air Arabia",
    # Asia-Pacific
    "Singapore Airlines", "Cathay Pacific", "ANA", "Japan Airlines", "Korean Air",
    "Asiana Airlines", "STARLUX Airlines", "EVA Air", "China Airlines",
    "Thai Airways", "Malaysia Airlines", "Garuda Indonesia", "Vietnam Airlines",
    "Philippine Airlines", "Bamboo Airways", "Air India", "Vistara", "IndiGo",
    "Cebu Pacific", "Scoot", "AirAsia", "Batik Air", "Hong Kong Airlines",
    "China Southern", "China Eastern", "Air China", "Hainan Airlines",
    "Juneyao Air", "Xiamen Air", "T'way Air", "Jin Air", "Zipair", "Peach Aviation",
    # Oceania
    "Qantas", "Air New Zealand", "Virgin Australia", "Fiji Airways",
    # Europe
    "Lufthansa", "British Airways", "Air France", "KLM", "Swiss", "Austrian Airlines",
    "Turkish Airlines", "Finnair", "SAS", "Iberia", "TAP Air Portugal",
    "Virgin Atlantic", "Aer Lingus", "ITA Airways", "Brussels Airlines",
    "LOT Polish Airlines", "Aeroflot", "Norwegian", "Wizz Air", "Ryanair",
    "easyJet", "Vueling", "Condor", "Discover Airlines", "Play", "Icelandair",
    # North America
    "Delta", "United Airlines", "American Airlines", "Alaska Airlines", "JetBlue",
    "Hawaiian Airlines", "Southwest Airlines", "Air Canada", "WestJet", "Porter Airlines",
    "Spirit Airlines", "Frontier Airlines", "Breeze Airways", "Aeromexico",
    # Latin America
    "LATAM", "Avianca", "Copa Airlines", "GOL", "Azul", "Sky Airline",
    # Africa
    "Ethiopian Airlines", "South African Airways", "Kenya Airways", "EgyptAir",
    "Royal Air Maroc", "RwandAir", "Air Mauritius",
]
_AIRLINE_QUERY_TEMPLATES = [
    "{a} inflight entertainment review",
    "{a} economy class review IFE",
    "{a} business class review screen",
    # generic — gate accepts any flight review now, so search for them too
    "{a} trip report",
]
# Aircraft-type queries (an IFE system usually ships per fleet type).
_AIRCRAFT_QUERIES = [
    "A330neo inflight entertainment review", "A330-900 inflight entertainment review",
    "A350-1000 inflight entertainment review", "787-9 inflight entertainment review",
    "787-10 inflight entertainment review", "777X inflight entertainment review",
    "A220 inflight entertainment review", "737 MAX inflight entertainment review",
    "E195-E2 inflight entertainment review",
]


def _build_generated_queries():
    seen = {q.lower() for q in YOUTUBE_QUERIES}
    out = []
    for a in GLOBAL_AIRLINES:
        for tpl in _AIRLINE_QUERY_TEMPLATES:
            q = tpl.format(a=a)
            if q.lower() not in seen:
                seen.add(q.lower()); out.append(q)
    for q in _AIRCRAFT_QUERIES:
        if q.lower() not in seen:
            seen.add(q.lower()); out.append(q)
    return out


# Curated queries first (highest-signal), then the broad generated net.
_CURATED_QUERY_COUNT = len(YOUTUBE_QUERIES)
YOUTUBE_QUERIES = YOUTUBE_QUERIES + _build_generated_queries()

# Known trusted source URLs to scrape directly (Tier 1 targets)
TRUSTED_SOURCES = [
    "https://simpleflying.com/?s=inflight+entertainment+2025",
    "https://simpleflying.com/?s=IFE+system+2025",
    "https://airlinegeeks.com/?s=inflight+entertainment",
    "https://thepointsguy.com/search?q=inflight+entertainment+2025",
]


class IFECrawler:
    """
    Auto-discovery crawler for IFE reviews.
    - Keyword-gates all results on title or description
    - Scores sources by reputation tier (Press / Creator / General)
    - Extracts structured specs: screen size, content count, WiFi type, controller
    - Targets 2025-2026 content primarily
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, verify_ssl: bool = False, api_key: str = ""):
        self.verify_ssl = verify_ssl
        self.api_key = api_key
        self._whisper_model = None
        self.visited: set = set()
        self.results: List[dict] = []
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

    # ── Public API ────────────────────────────────────────────────────────────

    # Max search.list queries per crawl. 100 units each; ~85 leaves headroom
    # under the 10k/day quota for channel pulls + videos.list metadata calls.
    QUERY_BUDGET = 85
    # Reserve at least this many slots for the rotating generated queries so the
    # broad airline net always advances, even though curated queries take priority.
    GENERATED_MIN = 30
    _ROTATE_FILE = Path(__file__).parent / ".query_offset"

    def _select_queries(self) -> List[str]:
        """Run the curated queries first (highest-signal); reserve a slice of the
        budget for a rotating window over the generated airline queries so coverage
        cycles through every airline across crawls instead of exceeding quota."""
        curated_all = YOUTUBE_QUERIES[:_CURATED_QUERY_COUNT]
        generated = YOUTUBE_QUERIES[_CURATED_QUERY_COUNT:]
        gen_slots = min(self.GENERATED_MIN, len(generated))
        curated = curated_all[:max(0, self.QUERY_BUDGET - gen_slots)]
        budget = max(0, self.QUERY_BUDGET - len(curated))
        if not generated or budget <= 0:
            return curated
        try:
            offset = int(self._ROTATE_FILE.read_text().strip())
        except Exception:
            offset = 0
        offset %= len(generated)
        # take a wrap-around slice of size `budget`
        picked = [generated[(offset + i) % len(generated)] for i in range(min(budget, len(generated)))]
        try:
            self._ROTATE_FILE.write_text(str((offset + budget) % len(generated)))
        except Exception:
            pass
        return curated + picked

    def auto_discover(self, existing_urls: set = None, max_results: int = 500, days_lookback: int = None) -> List[dict]:
        self.results = []
        if existing_urls:
            self.visited.update(existing_urls)

        published_after = None
        if days_lookback:
            from datetime import datetime, timedelta, timezone
            dt = datetime.now(timezone.utc) - timedelta(days=days_lookback)
            published_after = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if self.api_key:
            # Collect all video IDs across the query set (50 results each).
            # search.list costs 100 units/query and the daily quota is 10,000, so
            # we cap how many queries run per crawl and ROTATE through the big
            # generated list across successive crawls (persisted offset), so every
            # airline gets covered over a few days instead of blowing the quota.
            queries = self._select_queries()
            all_ids: List[str] = []
            seen_ids: set = set()
            for query in queries:
                ids = self._yt_search_api(query, limit=50, published_after=published_after)
                for vid_id in ids:
                    if vid_id not in seen_ids:
                        url = f"https://www.youtube.com/watch?v={vid_id}"
                        if url not in self.visited:
                            seen_ids.add(vid_id)
                            all_ids.append(vid_id)

            # Also pull recent uploads from every known IFE reviewer channel directly
            channel_ids: set = set()
            for channel_name, channel_id in KNOWN_IFE_CHANNELS.items():
                ids = self._yt_search_channel(channel_id, limit=50, published_after=published_after)
                for vid_id in ids:
                    if vid_id not in seen_ids:
                        url = f"https://www.youtube.com/watch?v={vid_id}"
                        if url not in self.visited:
                            seen_ids.add(vid_id)
                            all_ids.append(vid_id)
                            channel_ids.add(vid_id)

            # Batch-fetch structured metadata (1 API unit per 50 videos)
            details = self._yt_fetch_details(all_ids)

            for vid_id in all_ids:
                if len(self.results) >= max_results:
                    break
                item = details.get(vid_id)
                if not item:
                    continue
                url = f"https://www.youtube.com/watch?v={vid_id}"
                self.visited.add(url)
                # Trusted reviewer-channel videos bypass the strict keyword gate.
                entry = self._build_youtube_entry_from_api(vid_id, item, trusted=(vid_id in channel_ids))
                if entry:
                    self.results.append(entry)
        else:
            # Fallback: scrape YouTube search page + DDG
            for query in YOUTUBE_QUERIES:
                if len(self.results) >= max_results:
                    break
                video_ids = self._yt_search(query, limit=10)
                if not video_ids:
                    video_ids = self._ddg_search_youtube(query + " site:youtube.com", limit=8)
                for vid_id in video_ids:
                    if len(self.results) >= max_results:
                        break
                    url = f"https://www.youtube.com/watch?v={vid_id}"
                    if url in self.visited:
                        continue
                    self.visited.add(url)
                    entry = self._fetch_youtube(vid_id, url)
                    if entry:
                        self.results.append(entry)
                time.sleep(1.2)

        # Article pages via DuckDuckGo (unchanged)
        for query in AUTO_DISCOVERY_QUERIES:
            if len(self.results) >= max_results:
                break
            article_urls = self._ddg_search_articles(query, limit=4)
            for url in article_urls:
                if len(self.results) >= max_results:
                    break
                if url in self.visited:
                    continue
                self.visited.add(url)
                entry = self._fetch_article(url)
                if entry:
                    self.results.append(entry)
            time.sleep(1.2)

        return self.results

    def crawl_review_sites(self, airline=None, aircraft=None):
        return self.auto_discover()

    def save_results(self, path="ife_results.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)

    # ── DuckDuckGo search ─────────────────────────────────────────────────────

    def _ddg_post(self, query: str) -> Optional[BeautifulSoup]:
        try:
            resp = self.session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "b": "", "kl": "us-en"},
                timeout=14,
                verify=self.verify_ssl,
            )
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return None

    def _yt_search(self, query: str, limit: int = 10) -> List[str]:
        """Scrape YouTube search results directly — much higher yield than DDG site: queries."""
        import json as _json
        try:
            encoded = requests.utils.quote(query)
            resp = self.session.get(
                f"https://www.youtube.com/results?search_query={encoded}&sp=CAI%253D",
                timeout=14, verify=self.verify_ssl,
            )
            # YouTube embeds all search data as ytInitialData JSON in the page
            match = re.search(r'var ytInitialData\s*=\s*(\{.+?\});\s*(?:</script>|var )', resp.text, re.DOTALL)
            if not match:
                return []
            data = _json.loads(match.group(1))
            items = (
                data.get("contents", {})
                    .get("twoColumnSearchResultsRenderer", {})
                    .get("primaryContents", {})
                    .get("sectionListRenderer", {})
                    .get("contents", [{}])[0]
                    .get("itemSectionRenderer", {})
                    .get("contents", [])
            )
            ids = []
            for item in items:
                vid_id = item.get("videoRenderer", {}).get("videoId")
                if vid_id and vid_id not in ids:
                    ids.append(vid_id)
                    if len(ids) >= limit:
                        break
            return ids
        except Exception:
            return []

    def _yt_search_api(self, query: str, limit: int = 50, published_after: str = None) -> List[str]:
        """YouTube Data API v3 search — 100 quota units per call, up to 50 results."""
        params = {
            "part": "id",
            "q": query,
            "type": "video",
            "maxResults": min(limit, 50),
            "order": "relevance",
            "key": self.api_key,
        }
        if published_after:
            params["publishedAfter"] = published_after
        try:
            resp = self.session.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=params,
                timeout=15,
                verify=self.verify_ssl,
            )
            resp.raise_for_status()
            return [
                item["id"]["videoId"]
                for item in resp.json().get("items", [])
                if item.get("id", {}).get("videoId")
            ]
        except Exception:
            return []

    def _yt_search_channel(self, channel_id: str, limit: int = 50, published_after: str = None) -> List[str]:
        """List a channel's uploads via its uploads playlist (ID = channel ID with
        UC→UU). playlistItems costs 1 quota unit per 50 videos vs 100 for
        search.list, and paginates arbitrarily deep — so backfills can reach past
        the 50 most recent uploads. Items come newest-first; stops early once
        published_after is passed."""
        playlist_id = "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id
        ids: List[str] = []
        page_token = None
        try:
            while len(ids) < limit:
                params = {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": min(limit - len(ids), 50),
                    "key": self.api_key,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = self.session.get(
                    "https://www.googleapis.com/youtube/v3/playlistItems",
                    params=params,
                    timeout=15,
                    verify=self.verify_ssl,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("items", []):
                    vid = item.get("contentDetails", {}).get("videoId")
                    if not vid:
                        continue
                    published = (item.get("contentDetails", {}).get("videoPublishedAt")
                                 or item.get("snippet", {}).get("publishedAt") or "")
                    if published_after and published and published < published_after:
                        return ids
                    ids.append(vid)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            return ids
        except Exception:
            return ids

    def _yt_fetch_details(self, video_ids: List[str]) -> Dict[str, dict]:
        """Batch-fetch snippet + statistics for up to 50 videos per call (1 quota unit each)."""
        details: Dict[str, dict] = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            try:
                resp = self.session.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "snippet,statistics,contentDetails",
                        "id": ",".join(batch),
                        "key": self.api_key,
                    },
                    timeout=15,
                    verify=self.verify_ssl,
                )
                resp.raise_for_status()
                for item in resp.json().get("items", []):
                    details[item["id"]] = item
            except Exception:
                pass
        return details

    # Broader "is this a flight/cabin review" gate — accepts any video whose
    # title/description names an airline, aircraft, or review term (e.g.
    # "Icelandair 737 MAX 8 Economy Class Trip Report"), no IFE keyword needed.
    _AVIATION_REVIEW_TERMS = (
        "business class", "first class", "economy class", "premium economy",
        "class review", "cabin", "flight review", "trip report", "inflight",
        "in-flight", "in flight", "onboard", "on board", "seat review", "flew",
    )

    # Generic aviation vocabulary — catches reviews of carriers that aren't in
    # AIRLINE_KEYWORDS (Starlux, SriLankan, Asiana, IndiGo, …). Word-boundaried
    # so "flew"≠"flower"; includes the airplane emoji and CJK/Korean cabin terms.
    _AVIATION_GENERIC_RE = re.compile(
        r'\b(airlines?|airways|airline\b|air lines|flights?|flying|flew|aviation'
        r'|aircraft|airplanes?|aeroplanes?|planes?|airport|boarding|takeoff|landing'
        r'|fly|flies|flag carrier'
        r'|boeing|airbus|embraer|a3\d{2}s?|7\d7s?|dreamliners?)\b'
        r'|✈|飛行機|機内|机上|机内|搭乗|航空|エコノミー|ビジネスクラス|ファーストクラス'
        r'|항공|기내|비행',
        re.IGNORECASE,
    )

    # Carrier names shaped like "Air China" / "Oman Air" / "Riyadh Air" that
    # aren't in AIRLINE_KEYWORDS. Case-sensitive on purpose — proper nouns only,
    # so "air conditioner" or "fresh air" never match.
    _AIR_CARRIER_RE = re.compile(r'\b[Aa][Ii][Rr]\s+[A-Z]|\b[A-Z][a-zA-Z]+\s+[Aa][Ii][Rr]\b')
    # "Air <Word>" that is not an airline: sneakers, gadgets, military and events. These are
    # blanked before the carrier pattern runs so "Air Jordan 6" or "Air Force general" cannot
    # pass the gate on their own (real Air Force One flight videos still pass via "flight",
    # "onboard", trusted channels, etc.).
    _AIR_NOT_CARRIER_RE = re.compile(
        r'\b(?:nike\s+)?air\s+(?:jordans?|max|force|pods?|fryers?|purifiers?|conditioners?|coolers?|'
        r'tags?|buds?|hostess|shows?|raids?|quality|traffic|combat|guitar|compressors?|'
        r'mattress|bnb|drums?|rifles?|guns?|hockey|track|bags?|bikes?|filters?|pumps?|'
        r'strikes?|defen[cs]e|power|marshal|chief|cadets?)\b|\bjordans?\b|\bsneakers?\b|\bkicks\b',
        re.IGNORECASE)

    def _is_aviation_review(self, text: str) -> bool:
        t = text.lower()
        if _keyword_hits(t, AIRLINE_KEYWORDS) or _keyword_hits(t, AIRCRAFT_KEYWORDS):
            return True
        stripped = self._AIR_NOT_CARRIER_RE.sub(" ", text)
        if self._AVIATION_GENERIC_RE.search(stripped) or self._AIR_CARRIER_RE.search(stripped):
            return True
        return any(k in t for k in self._AVIATION_REVIEW_TERMS)

    def _build_youtube_entry_from_api(self, video_id: str, item: dict, trusted: bool = False) -> Optional[dict]:
        """Build a result entry from a YouTube Data API videos.list item.
        `trusted` relaxes the keyword gate for known reviewer channels."""
        snippet = item.get("snippet", {})
        title = snippet.get("title", "").strip()
        description = snippet.get("description", "").strip()

        duration_iso = item.get("contentDetails", {}).get("duration", "")
        if _is_spam_video(title, duration_iso, snippet.get("channelTitle", "")):
            return None

        title_match = self._has_ife_keyword(title)
        # Description-only: skip broad keywords (flight review, seat review, etc.)
        # so AI drama / movie review descriptions don't slip through via "life"/"wife".
        desc_match = self._has_ife_keyword(description, skip_broad=True)
        if not title_match and not desc_match:
            # Accept any genuine flight/cabin review even without an explicit
            # IFE keyword — most cabin reviews cover the IFE anyway. Title only:
            # long descriptions of unrelated videos routinely contain "flew",
            # "cabin", "on board", or an airline-name lookalike.
            if not self._is_aviation_review(title):
                return None

        published_at = snippet.get("publishedAt", "")
        try:
            year = int(published_at[:4])
        except (ValueError, IndexError):
            year = self._year_from_text(title + " " + description)

        url = f"https://www.youtube.com/watch?v={video_id}"
        combined = (title + " " + description).lower()

        trans_ok, excerpt, captions, full_transcript = False, None, [], ""
        if TRANSCRIPTS_AVAILABLE:
            trans_ok, excerpt, captions, full_transcript = self._get_transcript(video_id)

        search_text = combined + " " + full_transcript
        airlines_m = self._mentions(search_text, AIRLINE_KEYWORDS)
        aircraft_m = self._mentions(search_text, AIRCRAFT_KEYWORDS)
        detected = self._detect_system(search_text)
        # Only tag a system when it's explicitly named in the text; an airline-
        # based inference is just a guess and is kept separately, never displayed.
        guess = None if detected else infer_ife_system(airlines_m, aircraft_m)

        stats = item.get("statistics", {})
        duration_seconds = _iso_duration_seconds(duration_iso)
        return {
            "url":                  url,
            "title":                title[:150],
            "year":                 year,
            "published_at":         published_at,
            "duration_seconds":     duration_seconds,
            "is_short":             _is_short(title, duration_seconds, url),
            "channel_title":        snippet.get("channelTitle", ""),
            "view_count":           int(stats.get("viewCount", 0) or 0),
            "like_count":           int(stats.get("likeCount", 0) or 0),
            "ife_system":           detected,
            "ife_system_inferred":  False,
            "ife_system_guess":     guess,
            "chapters":             parse_chapters(description),
            "media_type":           "video",
            "airlines_mentioned":   airlines_m,
            "aircraft_mentioned":   aircraft_m,
            "ife_features":         self._features(search_text),
            "ife_specs":            _extract_specs(search_text),
            "transcript_available": trans_ok,
            "transcript_excerpt":   excerpt,
            "captions":             captions,
            "transcript_full":      full_transcript,
            "source_tier":          2,
            "source_name":          "Official" if is_official_promo(title, snippet.get("channelTitle", "")) else "Creator",
        }

    def _ddg_search_youtube(self, query: str, limit: int = 5) -> List[str]:
        """Fallback: DDG site:youtube.com search (sparse but works without JS)."""
        soup = self._ddg_post(query)
        if not soup:
            return []
        ids = []
        for a in soup.select("a.result__url, a.result__a"):
            href = a.get("href", "")
            vid_id = self._extract_yt_id(href)
            if vid_id and vid_id not in ids:
                ids.append(vid_id)
                if len(ids) >= limit:
                    break
        return ids

    def _ddg_search_articles(self, query: str, limit: int = 4) -> List[str]:
        soup = self._ddg_post(query)
        if not soup:
            return []
        urls = []
        skip = {"youtube.com", "twitter.com", "facebook.com", "instagram.com",
                "tiktok.com", "reddit.com", "wikipedia.org"}
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            url = self._resolve_ddg(href)
            if not url:
                continue
            if any(s in url for s in skip):
                continue
            if url not in urls:
                urls.append(url)
                if len(urls) >= limit:
                    break
        return urls

    def _resolve_ddg(self, href: str) -> Optional[str]:
        if "//duckduckgo.com/l/?" in href:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse("https:" + href).query)
                uddg = qs.get("uddg", [None])[0]
                return urllib.parse.unquote(uddg) if uddg else None
            except Exception:
                return None
        if href.startswith("http"):
            return href
        return None

    def _extract_yt_id(self, text: str) -> Optional[str]:
        m = re.search(r"(?:v=|youtu\.be/|/embed/|/v/|/shorts/)([A-Za-z0-9_-]{11})", text)
        return m.group(1) if m else None

    # ── YouTube fetch ─────────────────────────────────────────────────────────

    def _fetch_youtube(self, video_id: str, url: str) -> Optional[dict]:
        try:
            resp = self.session.get(url, timeout=12, verify=self.verify_ssl)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            title = (soup.title.string or "").replace(" - YouTube", "").strip()
            desc_meta = (
                soup.find("meta", {"name": "description"}) or
                soup.find("meta", {"property": "og:description"})
            )
            description = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""
            channel_meta = soup.find("link", {"itemprop": "name"})
            channel_title = channel_meta["content"].strip() if channel_meta and channel_meta.get("content") else ""

            if not self._has_ife_keyword(title) and not self._has_ife_keyword(description, skip_broad=True):
                # Same fallback as the API path: genuine flight/cabin reviews
                # nearly always cover the IFE even without an explicit keyword.
                if not self._is_aviation_review(title):
                    return None
            if _is_spam_video(title, channel_title=channel_title):
                return None

            combined = (title + " " + description).lower()
            date_meta = (soup.find("meta", {"itemprop": "datePublished"})
                         or soup.find("meta", {"itemprop": "uploadDate"}))
            published_at = date_meta["content"].strip() if date_meta and date_meta.get("content") else ""
            year = int(published_at[:4]) if published_at[:4].isdigit() else self._year_from_text(combined)

            trans_ok, excerpt, captions = False, None, []
            full_transcript = ""
            if TRANSCRIPTS_AVAILABLE:
                trans_ok, excerpt, captions, full_transcript = self._get_transcript(video_id)

            search_text = combined + " " + full_transcript
            airlines_m  = self._mentions(search_text, AIRLINE_KEYWORDS)
            aircraft_m  = self._mentions(search_text, AIRCRAFT_KEYWORDS)
            detected    = self._detect_system(search_text)
            guess       = None if detected else infer_ife_system(airlines_m, aircraft_m)

            return {
                "url":                  url,
                "title":                title[:150],
                "year":                 year,
                "published_at":         published_at,
                "is_short":             _is_short(title, None, url),
                "channel_title":        channel_title,
                "ife_system":           detected,
                "ife_system_inferred":  False,
                "ife_system_guess":     guess,
                "chapters":             parse_chapters(description),
                "media_type":           "video",
                "airlines_mentioned":   airlines_m,
                "aircraft_mentioned":   aircraft_m,
                "ife_features":         self._features(search_text),
                "ife_specs":            _extract_specs(search_text),
                "transcript_available": trans_ok,
                "transcript_excerpt":   excerpt,
                "captions":             captions,
                "transcript_full":      full_transcript,
                "source_tier":          2,
                "source_name":          "Official" if is_official_promo(title, channel_title) else "Creator",
            }
        except Exception:
            return None

    @staticmethod
    def _is_display_sentence(text: str) -> bool:
        t = text.strip()
        if len(t) < 35:
            return False
        if re.match(r'^[\[\(♪♫►>\s]', t) or re.match(r'^\[.*\]$', t):
            return False
        tl = t.lower()
        # Strong IFE signals only — weak words like "flight"/"cabin"/"class" made
        # greetings ("first flight.", "enjoy your flight.") pass as IFE captions.
        ife_ctx = [
            "entertainment", "screen", "display", "monitor", "seatback", "seat back",
            "touchscreen", "touch screen", "4k", "oled", "amoled", "uhd", "resolution",
            "inch", "wifi", "wi-fi", "bluetooth", "headphone", "usb", "charging port",
            "streaming", "content library", "content", "remote control", "movies",
            "tv show", "tv shows", "tv series", "shows", "selection of movies",
            "ife", "panasonic", "thales", "rave", "safran", "viasat", "starlink",
            "krisworld", "astrova", "avant", "oryx", "studiocx",
            "video on demand", "watch party", "map view", "flight map",
            "seat-to-seat chat", "seat-to-seat messaging", "inflight connectivity",
            "4k oled", "mini-led screen", "mini-led",
            "divertissement", "bordunterhaltung", "écran",
            "娱乐", "エンタメ", "엔터테인먼트", "entretenimiento", "entretenimento",
        ]
        return any(kw in tl for kw in ife_ctx)

    @staticmethod
    def _segs_to_caps(segs: list, score_kws: list):
        """Convert raw transcript segments to (excerpt, captions, full_text)."""
        full = " ".join(s["text"] for s in segs)

        def seg_score(idx):
            chunk = " ".join(
                segs[j]["text"]
                for j in range(max(0, idx - 1), min(len(segs), idx + 2))
            ).lower()
            return sum(1 for kw in score_kws if kw in chunk)

        is_ok = IFECrawler._is_display_sentence
        # Only pick IFE-scoring segments that read as an IFE sentence. Do NOT pad
        # with evenly-spaced generic segments — fewer, relevant captions beat
        # filler like "first flight." / "Good".
        order = sorted(range(len(segs)), key=lambda i: -seg_score(i))
        chosen_idx = []
        for i in order:
            if seg_score(i) == 0:
                break
            if not is_ok(segs[i]["text"].strip()):
                continue
            if not any(abs(i - j) < 15 for j in chosen_idx):
                chosen_idx.append(i)
            if len(chosen_idx) >= 5:
                break
        chosen_idx.sort()
        excerpt_text = None
        for i in order:
            t = segs[i]["text"].strip()
            if is_ok(t):
                excerpt_text = t
                break
        if not excerpt_text:
            excerpt_text = segs[order[0]]["text"].strip() if order else segs[0]["text"].strip()
        excerpt = excerpt_text + ("…" if len(full) > len(excerpt_text) else "")

        caps = []
        for i in chosen_idx[:5]:
            s = segs[i]
            t = s["text"].strip()
            if not is_ok(t):
                continue
            raw = int(s["start"])
            m, sec = raw // 60, raw % 60
            caps.append({"timestamp": f"{m}:{sec:02d}", "start_seconds": raw, "text": t})

        return excerpt, caps, full

    def _get_transcript(self, video_id: str):
        score_kws = (
            [kw for kws in IFE_FEATURE_KEYWORDS.values() for kw in kws]
            + [p for patterns in IFE_SYSTEM_PATTERNS.values() for p in patterns]
        )
        # ── 1. YouTube transcript API ─────────────────────────────────────────
        ip_blocked = False
        try:
            api = YouTubeTranscriptApi()
            try:
                fetched = api.fetch(video_id, languages=[
                    "en", "en-US", "en-GB",
                    "fr", "de", "ja", "ko", "zh", "zh-TW", "zh-CN",
                    "es", "pt", "tr", "it", "ar", "nl", "fi", "no",
                ])
            except Exception:
                tlist = api.list(video_id)
                auto = next(iter(tlist._generated_transcripts.values()), None)
                if auto:
                    fetched = auto.fetch()
                else:
                    raise
            segs = [{"text": s.text, "start": s.start} for s in fetched]
            if segs:
                excerpt, caps, full = self._segs_to_caps(segs, score_kws)
                return True, excerpt, caps, full
        except Exception as e:
            if "IpBlocked" in type(e).__name__:
                ip_blocked = True

        # ── 2. Whisper fallback (skipped when IP-blocked — yt-dlp would fail too) ──
        if not ip_blocked:
            return self._whisper_transcript(video_id, score_kws)

        return False, None, [], ""

    def _load_whisper(self):
        """Lazy-load the local Whisper model (cached on self after first call)."""
        if self._whisper_model is None:
            import whisper as _whisper
            import os as _os
            size = _os.environ.get("WHISPER_MODEL", "base")
            self._whisper_model = _whisper.load_model(size)
        return self._whisper_model

    def _whisper_transcript(self, video_id: str, score_kws: list):
        """Download audio via yt-dlp and transcribe with local Whisper model."""
        try:
            import yt_dlp
            import tempfile
            import os as _os

            class _Q:
                errors = []
                def debug(self, m): pass
                def warning(self, m): pass
                def error(self, m): self.errors.append(m.lower())

            logger = _Q()
            url = f"https://www.youtube.com/watch?v={video_id}"
            with tempfile.TemporaryDirectory() as tmpdir:
                ydl_opts = {
                    "format": "worstaudio/worst",
                    "outtmpl": _os.path.join(tmpdir, "%(id)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                    "nocheckcertificate": True,
                    "logger": logger,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                # Bot-detected or private — bail silently
                errs = " ".join(logger.errors)
                if any(s in errs for s in ("sign in", "private video", "not a bot", "unavailable")):
                    return False, None, [], ""

                files = _os.listdir(tmpdir)
                if not files:
                    return False, None, [], ""
                audio_path = _os.path.join(tmpdir, files[0])

                model = self._load_whisper()
                result = model.transcribe(audio_path, verbose=False)

            segments = result.get("segments") or []
            if not segments:
                return False, None, [], ""

            segs = [{"text": seg["text"], "start": seg["start"]} for seg in segments]
            excerpt, caps, full = self._segs_to_caps(segs, score_kws)
            return True, excerpt, caps, full
        except Exception:
            return False, None, [], ""

    # ── Article fetch ─────────────────────────────────────────────────────────

    def _fetch_article(self, url: str) -> Optional[dict]:
        # Only accept articles from known aviation press (T1) or creator blogs (T2).
        # General/unknown sites (T3) are excluded — internal sources cover those.
        tier = source_tier(url)
        if tier == 3:
            return None

        try:
            resp = self.session.get(url, timeout=10, verify=self.verify_ssl)
            resp.raise_for_status()
            # requests defaults to ISO-8859-1 when a server omits a charset header,
            # which mangles smart quotes/em-dashes in UTF-8 pages — sniff instead.
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")

            title = (soup.title.string or "").strip()
            desc_meta = (
                soup.find("meta", {"name": "description"}) or
                soup.find("meta", {"property": "og:description"})
            )
            description = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""

            if not self._has_ife_keyword(title) and not self._has_ife_keyword(description):
                return None

            raw_text   = soup.get_text(separator=" ")
            text       = raw_text.lower()
            year       = self._year_from_meta(soup) or self._year_from_text(text)
            airlines_m = self._mentions(text, AIRLINE_KEYWORDS)
            aircraft_m = self._mentions(text, AIRCRAFT_KEYWORDS)
            detected   = self._detect_system(text)
            guess      = None if detected else infer_ife_system(airlines_m, aircraft_m)

            return {
                "url":                  url,
                "title":                title[:150],
                "year":                 year,
                "ife_system":           detected,
                "ife_system_inferred":  False,
                "ife_system_guess":     guess,
                "media_type":           "article",
                "airlines_mentioned":   airlines_m,
                "aircraft_mentioned":   aircraft_m,
                "ife_features":         self._features(text),
                "ife_specs":            _extract_specs(text),
                "transcript_available": False,
                "transcript_excerpt":   _article_excerpt(raw_text),
                "article_quotes":       _article_quotes(raw_text),
                "article_text":         _article_paragraphs(soup),
                "captions":             [],
                "source_tier":          tier,
                "source_name":        TIER_LABELS[tier],
            }
        except Exception:
            return None

    # ── Text helpers ──────────────────────────────────────────────────────────

    def _has_ife_keyword(self, text: str, skip_broad: bool = False) -> bool:
        t = text.lower()
        for kw in IFE_TITLE_KEYWORDS:
            if skip_broad and kw in _IFE_TITLE_ONLY_KEYWORDS:
                continue
            if kw in t:
                return True
        return bool(_IFE_WORD_RE.search(t))

    def _detect_system(self, text: str) -> Optional[str]:
        t = text.lower()
        for name, patterns in IFE_SYSTEM_PATTERNS.items():
            if any(p in t for p in patterns):
                return name
        return None

    def _features(self, text: str) -> Dict[str, bool]:
        return {f: True for f, kws in IFE_FEATURE_KEYWORDS.items() if any(kw in text for kw in kws)}

    def _mentions(self, text: str, keywords: List[str]) -> List[Dict]:
        out = [{"keyword": kw, "mentions": len(_keyword_re(kw).findall(text))}
               for kw in _keyword_hits(text, keywords)]
        return sorted(out, key=lambda x: x["mentions"], reverse=True)[:5]

    def _year_from_meta(self, soup: BeautifulSoup) -> Optional[int]:
        for attr in ("article:published_time", "datePublished", "publish_date", "date"):
            meta = soup.find("meta", {"property": attr}) or soup.find("meta", {"name": attr})
            if meta and meta.get("content"):
                m = re.search(r"(202[0-9])", meta["content"])
                if m:
                    return int(m.group(1))
        return None

    def _year_from_text(self, text: str) -> Optional[int]:
        # Prefer most recent year found
        for y in ["2026", "2025", "2024", "2023", "2022"]:
            if y in text:
                return int(y)
        m = re.search(r"(202[0-9])", text)
        return int(m.group(1)) if m else None
