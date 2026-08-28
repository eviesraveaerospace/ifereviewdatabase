"""Targeted purge: remove junk videos and non-IFE press articles."""
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

BAD_TITLE_SUBSTRINGS = [
    "DALE WAS ACTING REALLY STRANGE",
    "Kannur Squad Review",
    "Book-The-Cook",
    "book the cook",
    "Book the Cook",
    "Yue Yuting",
]

_JUNK_RE = re.compile(
    r'\bzerg\b'
    r'|\bevolution\s+system\b'
    r'|\bant\s+(soldier|empire|colony|queen)\b'
    r'|\bhatch(es|ing)?\s+\d'
    r'|\bmarried\s+the\s+(ceo|boss|president|king|prince|tycoon)\b'
    r'|\bdiscovered\s+\d+\s*bros\b'
    r'|\bsecret\s+heiress\b'
    r'|\breborn\s+in\s+\d{4}\b'
    r'|\bnecromancer\b'
    r'|\bweak(ling|est)?\s+awaken'
    r'|\binfinite\s+weapon\b'
    r'|\bsystem\s+builds?\b'
    r'|\blanded\s+a\s+burning\s+jet\b'
    r'|\bpoisoned\s+trial\b'
    r'|\bhorror\s+movie\b'
    r'|\bfree\s+horror\b'
    r'|\bsailing\s+catamaran\b'
    r'|\bsuperyacht\b'
    r'|\bcruise\s+ship\s+review\b'
    r'|\broblox\b'
    r'|\bworld\s+news\s+tonight\b'
    r'|\btim\s+dillon\s+show\b'
    r'|\bnational\s+geo(graphic)?\b'
    r'|\bnikola\s+tesla\b'
    r'|\bdrone\s+review\b'
    r'|\bdanish\s+practice\b'
    r'|\bswedish\s+practice\b'
    r'|\bnorwegian\s+practice\b'
    r'|\benglish\s+practice\s+lesson\b'
    r'|\blearn\s+russian\b'
    r'|\bwife\s+(offered|threw|poisoned|lied)\b'
    r'|\bhusband\s+lied\b'
    r'|\bshe\s+was\s+a\s+substitute\b'
    r'|\bmy\s+(rich\s+)?wife\s+(offered|threw|paid)\b'
    r'|\bbankrupt\s+ceo\b'
    r'|\bceo\s+heard\s+my\s+mind\b'
    r'|\bmillionaire\s+just\s+by\b'
    r'|\bthe\s+f.rank\s+weakling\b'
    r'|\boverpowered\s+(necromancer|system|bloodline)\b'
    r'|\bdiscovered.*castle\b'
    r'|\bsquad\s+review\s+@\w'
    r'|\bmeet,?\s+marry,?\s+murder\b'
    r'|\bpure\s+temptation\b'
    r'|\badulterous\s+husband\b'
    r'|\bhas\s+the\s+general\s+completely\s+crazy\b',
    re.IGNORECASE,
)

# IFE-specific terms that should appear in a relevant press article
_IFE_CONTENT_KEYWORDS = [
    "inflight entertainment", "in-flight entertainment", "ife system",
    "seatback", "seat-back", "seat back screen", "entertainment screen",
    "panasonic", "thales", "safran", "rave ife", "krisworld", "oryx",
    "studiocx", "wifi", "wi-fi", "onboard wifi", "gogo", "viasat",
    "entertainment system", "video on demand"
]


def _is_spam(title):
    if len(re.findall(r'#\w+', title)) >= 4:
        return True
    tl = title.lower()
    return "#shorts" in tl or "#short " in tl or "| shorts" in tl


# Hotel/resort/land-lodging content with no aviation context at all
_HOTEL_RE = re.compile(r'\bhotels?\b|\bresorts?\b|\bairbnb\b|\bvillas?\b|\bhostels?\b', re.I)
_AVIATION_RE = re.compile(
    r'flight|airline|airways|air lines|business class|first class|economy|premium economy'
    r'|boeing|airbus|\ba3\d{2}\b|\b7\d7\b|dreamliner|lounge|airport|\bife\b|inflight|in-flight',
    re.I,
)


def _is_offtopic(title):
    return bool(_HOTEL_RE.search(title)) and not _AVIATION_RE.search(title)


# ── Crawler relevance gate, re-applied to cached videos ──────────────────────
# Uses the same (word-boundaried) gate the crawler now applies at crawl time,
# so junk admitted by the old substring gate gets purged retroactively.
import ife_crawler as _ic

_gate = _ic.IFECrawler.__new__(_ic.IFECrawler)   # helpers only, no network setup
_TRUSTED_CHANNELS = {n.lower().replace(" (official)", "") for n in _ic.KNOWN_IFE_CHANNELS}


def _video_fails_gate(r):
    if r.get("media_type") != "video":
        return False
    title = r.get("title", "")
    channel = (r.get("channel_title") or "").strip().lower()
    return not (
        channel in _TRUSTED_CHANNELS
        or _gate._has_ife_keyword(title)
        or _gate._is_aviation_review(title)
        or bool(r.get("ife_system"))
    )


def _press_has_ife_content(r):
    """Return True if press article actually covers IFE topics."""
    combined = " ".join([
        r.get("title", ""),
        r.get("excerpt", "") or "",
        r.get("content", "") or "",
    ]).lower()
    return any(kw in combined for kw in _IFE_CONTENT_KEYWORDS) or bool(r.get("ife_features"))


with open("ife_cache.json", encoding="utf-8") as f:
    data = json.load(f)

before = len(data["reviews"])
kept, removed = [], []

for r in data["reviews"]:
    title = r.get("title", "")
    if r.get("media_type") == "internal":
        kept.append(r)          # team-entered reviews are never auto-purged
        continue
    is_press = r.get("source_tier") == 1

    bad = (
        _is_spam(title)
        or _JUNK_RE.search(title)
        or _is_offtopic(title)
        or any(s.lower() in title.lower() for s in BAD_TITLE_SUBSTRINGS)
        or (is_press and not _press_has_ife_content(r))
        or _video_fails_gate(r)
    )
    if bad:
        removed.append(f"[{'press' if is_press else 'video'}] {title[:80]}")
    else:
        kept.append(r)

data["reviews"] = kept

with open("ife_cache.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"Removed {before - len(kept)} entries ({len(kept)} remain)")
for t in removed:
    print(f"  - {t}")
