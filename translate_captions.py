"""
Translate non-English transcripts, captions, titles and comments to English
using Google Translate (via the free `deep-translator` library — no API key).

Two passes:

1. Whole-transcript pass. For every review whose `transcript_full` is not
   English (langdetect, with a non-Latin-script fast path) an English
   `transcript_full_en` is produced (translated in ~4.5k-char chunks) and
   `transcript_lang` recorded. The English text is then used to rebuild the
   searchable `transcript_excerpt` / `captions` (the originals were derived
   with English IFE keywords, so non-English transcripts produced nothing) and
   to re-run system/feature/airline/aircraft detection. Original `transcript_full`
   and `transcript_excerpt_orig` are kept untouched.

2. Per-line pass (legacy). Caption lines, titles and YouTube comments in a
   non-Latin script get `text_en` / `title_en` + `lang`. Pass --all to also send
   accented-Latin lines through auto-detect.

Saving is merge-based: the cache is re-read right before every write and only
this script's fields are patched in, so it is safe to run while
backfill_transcripts.py is also writing to ife_cache.json.

Run:  python translate_captions.py                 (both passes)
      python translate_captions.py --limit 30      (test on the first 30 items)
      python translate_captions.py --all           (also accented-Latin lines)
      python translate_captions.py --prefer emirates   (do that airline first)
      python translate_captions.py --lines-only    (skip whole-transcript pass)
"""
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from deep_translator import GoogleTranslator

CACHE = Path(__file__).parent / "ife_cache.json"
CHUNK = 4500            # Google Translate web endpoint limit is 5000 chars
MIN_TRANSCRIPT = 200    # ignore stubs
SAVE_EVERY = 5          # transcripts per checkpoint

# A line with several non-Latin chars (CJK / Arabic / Cyrillic / Thai / Indic …)
# is certainly non-English. Emoji, punctuation and Latin-1 accents are excluded
# from this test so English-with-emoji lines aren't flagged.
_NONLATIN = re.compile(r"[^\x00-\x7FÀ-ɏ -➿\U0001F000-\U0001FAFF]")

TRANSCRIPT_KEYS = ("transcript_full_en", "transcript_lang", "transcript_excerpt",
                   "transcript_excerpt_orig", "captions",
                   "ife_system", "ife_system_inferred", "ife_system_guess", "ife_features",
                   "ife_specs", "airlines_mentioned", "aircraft_mentioned")


def _has_nonlatin(s):
    return len(_NONLATIN.findall(s or "")) >= 2


def _guess_lang(s):
    """Best-effort source-language guess from character ranges (no network)."""
    s = s or ""
    if re.search(r"[぀-ヿ]", s):
        return "ja"                                  # hiragana/katakana
    if re.search(r"[가-힯]", s):
        return "ko"                                  # hangul
    if re.search(r"[؀-ۿ]", s):
        return "ar"                                  # arabic
    if re.search(r"[Ѐ-ӿ]", s):
        return "ru"                                  # cyrillic
    if re.search(r"[ఀ-౿]", s):
        return "te"                                  # telugu
    if re.search(r"[ऀ-ॿ]", s):
        return "hi"                                  # devanagari
    if re.search(r"[฀-๿]", s):
        return "th"                                  # thai
    if re.search(r"[一-鿿]", s):
        return "zh"                                  # cjk (chinese, or shared kanji)
    return "und"


def _needs_translation(s, include_latin):
    s = (s or "").strip()
    if len(s) < 3:
        return False
    if _has_nonlatin(s):
        return True
    if include_latin:
        # accented Latin that might be non-English (café, écran, für…)
        return bool(re.search(r"[À-ÿ]", s))
    return False


# ── whole-transcript language detection ──────────────────────────────────────

_SCRIPTS = {
    "latin": r"[A-Za-zÀ-ɏ]", "cjk": r"[一-鿿]", "kana": r"[぀-ヿ]", "hangul": r"[가-힯]",
    "arabic": r"[؀-ۿ]", "cyrillic": r"[Ѐ-ӿ]", "devanagari": r"[ऀ-ॿ]", "bengali": r"[ঀ-৿]",
    "thai": r"[฀-๿]", "tamil": r"[஀-௿]", "telugu": r"[ఀ-౿]", "greek": r"[Ͱ-Ͽ]",
}


_SCRIPT_GROUP = {"cjk": "ja", "kana": "ja"}   # Japanese legitimately mixes both


def is_garbage_transcript(text):
    """Whisper hallucination detector. Real speech is written in one script
    (plus some Latin for names) and langdetect calls the same language with
    high confidence across the whole text; noise mixes scripts, flips language
    between windows, or is a repeated token like '[музыка] [музыка] …'."""
    sample = (text or "")[:6000]
    if len(sample) < MIN_TRANSCRIPT:
        return False
    # 1. several non-Latin script groups
    letters = sum(len(re.findall(p, sample)) for p in _SCRIPTS.values()) or 1
    groups = set()
    for k, p in _SCRIPTS.items():
        if k != "latin" and len(re.findall(p, sample)) / letters > 0.08:
            groups.add(_SCRIPT_GROUP.get(k, k))
    if len(groups) >= 2:
        return True
    # 2. repetition (music tags, "1.5% 1.5% …", one looping phrase)
    from collections import Counter
    toks = re.findall(r"\S+", sample)
    if len(toks) >= 40:
        if len(set(toks)) / len(toks) < 0.15:
            return True
        if sum(c for _, c in Counter(toks).most_common(3)) / len(toks) > 0.5:
            return True
    # 3. language flips / low confidence across windows
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        win = 500
        starts = list(range(0, max(1, len(sample) - win + 1), max(win, len(sample) // 6)))
        langs, low = [], 0
        for i in starts:
            chunk = sample[i:i + win] if len(sample) >= win else sample
            try:
                best = detect_langs(chunk)[0]
            except Exception:
                return True                      # nothing detectable at all
            langs.append(best.lang)
            low += best.prob < 0.90
        if Counter(langs).most_common(1)[0][1] / len(langs) < 0.6:
            return True                          # no majority language
        if low > len(langs) / 2:
            return True                          # mostly low-confidence
    except ImportError:
        pass
    return False


def detect_transcript_lang(text):
    """Return ISO-639-1 code for a transcript, 'en' if English (or unsure),
    'garbage' for Whisper noise that should not be translated."""
    text = (text or "").strip()
    if len(text) < MIN_TRANSCRIPT:
        return "en"
    if is_garbage_transcript(text):
        return "garbage"
    sample = text[:6000]
    if len(_NONLATIN.findall(sample)) > len(sample) * 0.15:
        g = _guess_lang(sample)
        return g if g != "und" else "auto"
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        best = detect_langs(sample)[0]
        # Transcripts are noisy (music tags, names); require a confident call.
        if best.lang != "en" and best.prob >= 0.90:
            return best.lang
    except Exception:
        pass
    return "en"


def _chunks(text, size=CHUNK):
    """Split on sentence-ish boundaries into <= size chunks."""
    out, buf = [], ""
    for piece in re.split(r"(?<=[.!?।。])\s+", text):
        if len(buf) + len(piece) + 1 > size and buf:
            out.append(buf)
            buf = piece
        else:
            buf = (buf + " " + piece).strip()
    if buf:
        out.append(buf)
    # hard-split anything still too long (no punctuation at all)
    final = []
    for c in out:
        while len(c) > size:
            final.append(c[:size])
            c = c[size:]
        final.append(c)
    return final


def _looks_like_error(s):
    """deep-translator sometimes returns Google's HTML error page as text."""
    s = (s or "").lower()
    return "error 500" in s or "that’s an error" in s or "that's an error" in s \
        or "too many requests" in s


_ARGOS_CACHE = {}


def _argos(lang):
    """Return an installed Argos offline lang->en translation, or None."""
    if lang in _ARGOS_CACHE:
        return _ARGOS_CACHE[lang]
    tr = None
    try:
        from argostranslate import translate as at
        src = next((l for l in at.get_installed_languages() if l.code == lang), None)
        dst = next((l for l in at.get_installed_languages() if l.code == "en"), None)
        if src and dst:
            tr = src.get_translation(dst)
    except Exception:
        tr = None
    _ARGOS_CACHE[lang] = tr
    return tr


def google_translate(translator, text):
    """Chunked Google (deep-translator) translation with retries + error guard."""
    parts = []
    for c in _chunks(text, 1500):
        for attempt in range(3):
            try:
                out = translator.translate(c) or ""
                if _looks_like_error(out):
                    raise RuntimeError("google returned an error page")
                parts.append(out)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
        time.sleep(0.3)
    return " ".join(p.strip() for p in parts if p)


def translate_text(translator, text, lang="auto"):
    """Offline Argos when a pack for `lang` is installed, else Google."""
    tr = _argos(lang) if lang not in ("auto", "und") else None
    if tr is not None:
        try:
            parts = [tr.translate(c) for c in _chunks(text, 2500)]
            out = " ".join(p.strip() for p in parts if p)
            if out.strip():
                return out
        except Exception as e:      # e.g. missing stanza sentencizer for a pack
            print(f"    argos {lang} failed ({type(e).__name__}), falling back to Google")
    return google_translate(translator, text)


def _rederive_from_english(r, en):
    """Rebuild excerpt/captions and re-run detection using the English text."""
    from backfill_transcripts import enrich_from_transcript, _SCORE_KWS
    from ife_crawler import IFECrawler
    sents = [s for s in re.split(r"(?<=[.!?])\s+", en) if s.strip()]
    segs = [{"text": s.strip(), "start": float(i * 5)} for i, s in enumerate(sents)]
    excerpt, caps, _ = IFECrawler._segs_to_caps(segs, _SCORE_KWS)
    for c in caps:
        c["lang"] = "en"
    if "transcript_excerpt_orig" not in r:
        r["transcript_excerpt_orig"] = r.get("transcript_excerpt")
    r["transcript_excerpt"] = excerpt
    r["captions"] = caps
    # enrich reads transcript_full — feed it English temporarily
    orig = r.get("transcript_full")
    r["transcript_full"] = en
    try:
        enrich_from_transcript(r)
    finally:
        r["transcript_full"] = orig


def merge_save(data, touched_urls, keys):
    """Re-read cache from disk and patch only `keys` for `touched_urls`."""
    fresh = json.loads(CACHE.read_text(encoding="utf-8"))
    by_url = {r.get("url"): r for r in data.get("reviews", [])}
    for fr in fresh.get("reviews", []):
        u = fr.get("url")
        if u in touched_urls and u in by_url:
            src = by_url[u]
            for k in keys:
                if k in src:
                    fr[k] = src[k]
                elif k in fr:
                    del fr[k]          # key removed in memory (e.g. reverted translation)
    CACHE.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")


def translate_transcripts(data, translator, prefer=None, limit=None):
    reviews = data.get("reviews", [])
    todo = []
    for r in reviews:
        full = r.get("transcript_full") or ""
        if len(full) < MIN_TRANSCRIPT or r.get("transcript_full_en"):
            continue
        if r.get("transcript_lang") in ("en", "garbage"):
            continue
        lang = detect_transcript_lang(full)
        r["transcript_lang"] = lang
        if lang not in ("en", "garbage"):
            todo.append(r)

    if prefer:
        p = prefer.lower()

        def rank(r):
            t = (r.get("title") or "").lower()
            al = r.get("airlines_mentioned") or []
            top = max(al, key=lambda k: k["mentions"])["keyword"] if al else ""
            return 0 if (p in t or top == p) else 1
        todo.sort(key=rank)
    if limit:
        todo = todo[:limit]

    # Persist the 'en'/'garbage' markers so detection is not repeated next run.
    marked = {r["url"] for r in reviews if r.get("transcript_lang") in ("en", "garbage")}
    merge_save(data, marked, ("transcript_lang",))

    print(f"Transcripts to translate: {len(todo)}")
    done = fail = 0
    touched = set()
    langs = {}
    for n, r in enumerate(todo, 1):
        try:
            en = translate_text(translator, r["transcript_full"], r.get("transcript_lang", "auto"))
            if not en.strip():
                raise RuntimeError("empty translation")
            if is_garbage_transcript(en) or (len(en) < len(r["transcript_full"]) * 0.15) \
                    or "cookies to improve your browsing" in en.lower():
                r["transcript_lang"] = "garbage"
                touched.add(r["url"])
                print(f"  ~ [{n}/{len(todo)}] garbage (skipped) {(r.get('title') or '')[:60]}")
                continue
            r["transcript_full_en"] = en
            _rederive_from_english(r, en)
            touched.add(r["url"])
            done += 1
            langs[r["transcript_lang"]] = langs.get(r["transcript_lang"], 0) + 1
            print(f"  [{n}/{len(todo)}] {r['transcript_lang']:>4}  {(r.get('title') or '')[:60]}")
        except Exception as e:
            fail += 1
            print(f"  ! [{n}/{len(todo)}] failed {r['url']}: {type(e).__name__} {str(e)[:80]}")
        if touched and n % SAVE_EVERY == 0:
            merge_save(data, touched, TRANSCRIPT_KEYS)
            touched.clear()
    if touched:
        merge_save(data, touched, TRANSCRIPT_KEYS)
    print(f"Transcripts done: {done} (failed {fail}) by language: "
          f"{dict(sorted(langs.items(), key=lambda x: -x[1]))}")


# ── per-line pass (titles / caption lines / comments) ───────────────────────

LINE_KEYS = ("title_en", "title_lang", "captions", "yt_comments")


def translate_lines(data, translator, include_latin=False, limit=None):
    reviews = data.get("reviews", [])
    pending = []
    for r in reviews:
        t = (r.get("title") or "").strip()
        if not r.get("title_en") and r.get("title_lang") != "en":
            if _needs_translation(t, include_latin):
                pending.append((r, t, "title_en", "title_lang"))
            elif len(t) >= 3 and not _has_nonlatin(t):
                r["title_lang"] = "en"
    for r in reviews:
        for c in (r.get("captions") or []) + (r.get("yt_comments") or []):
            if c.get("text_en") or c.get("lang") == "en":
                continue
            t = (c.get("text") or "").strip()
            if _needs_translation(t, include_latin):
                pending.append((c, t, "text_en", "lang"))
            elif len(t) >= 3 and not _has_nonlatin(t):
                c["lang"] = "en"

    if limit:
        pending = pending[:limit]
    print(f"Items to translate (titles + captions + comments): {len(pending)}")
    touched = {r["url"] for r in reviews}   # 'en' markers touch everything
    if not pending:
        merge_save(data, touched, LINE_KEYS)
        print("Nothing to translate.")
        return

    done = fail = 0
    langs = {}
    for n, (obj, t, en_field, lang_field) in enumerate(pending, 1):
        try:
            guess = _guess_lang(t)
            ar = _argos(guess) if guess != "und" else None
            en = ar.translate(t) if ar is not None else translator.translate(t)
            if _looks_like_error(en):
                raise RuntimeError("google returned an error page")
            obj[lang_field] = guess
            if en and en.strip().lower() != t.strip().lower():
                obj[en_field] = en
                langs[obj[lang_field]] = langs.get(obj[lang_field], 0) + 1
            done += 1
        except Exception as e:
            fail += 1
            print(f"  ! item {n} failed: {type(e).__name__} {str(e)[:80]}")
        if n % 20 == 0:
            merge_save(data, touched, LINE_KEYS)
            print(f"  [{n}/{len(pending)}] translated (fail={fail})")
        time.sleep(0.1)  # be gentle with the free endpoint

    merge_save(data, touched, LINE_KEYS)
    print(f"Lines done: {done} (failed {fail}) by language: "
          f"{dict(sorted(langs.items(), key=lambda x: -x[1]))}")


def main():
    argv = sys.argv[1:]
    include_latin = "--all" in argv
    lines_only = "--lines-only" in argv
    limit = prefer = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except Exception:
            pass
    if "--prefer" in argv:
        try:
            prefer = argv[argv.index("--prefer") + 1]
        except Exception:
            pass

    data = json.loads(CACHE.read_text(encoding="utf-8"))
    translator = GoogleTranslator(source="auto", target="en")
    if not lines_only:
        translate_transcripts(data, translator, prefer=prefer, limit=limit)
    translate_lines(data, translator, include_latin=include_latin, limit=limit)
    print(f"Saved {CACHE.name}.")


if __name__ == "__main__":
    main()
