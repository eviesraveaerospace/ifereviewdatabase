import json
import os
import re
from datetime import datetime
from ife_crawler import IFECrawler

# CJK / Hangul / Thai / Arabic / Cyrillic — a title with 3+ such chars is a
# non-English-language upload.
_NONLATIN_RE = re.compile(r'[぀-ヿ㐀-鿿가-힯฀-๿؀-ۿЀ-ӿ]')


def _is_nonlatin_title(title: str) -> bool:
    return len(_NONLATIN_RE.findall(title or "")) >= 3

# ── VADER sentiment (optional — degrades gracefully if nltk missing) ──────────
try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer as _VADER
    import nltk as _nltk
    try:
        _sia = _VADER()
    except LookupError:
        _nltk.download("vader_lexicon", quiet=True)
        _sia = _VADER()
    _VADER_OK = True
except ImportError:
    _VADER_OK = False


def _compute_sentiment(r: dict) -> tuple:
    if not _VADER_OK:
        return "neutral", 0.0
    text = " ".join(filter(None, [
        r.get("title", ""),
        r.get("transcript_excerpt") or "",
        r.get("excerpt") or "",
    ]))
    if not text.strip():
        return "neutral", 0.0
    compound = _sia.polarity_scores(text)["compound"]
    if compound >= 0.05:
        return "positive", round(compound, 3)
    if compound <= -0.05:
        return "negative", round(compound, 3)
    return "neutral", round(compound, 3)


class IFEDataManager:
    """Manages cached IFE review data with filtering and pagination."""

    def __init__(self, cache_file="ife_cache.json"):
        self.cache_file = cache_file
        self.data = self.load_cache()

    def load_cache(self):
        if os.path.exists(self.cache_file):
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"reviews": [], "last_updated": None}

    def _dedupe_reviews(self):
        """Collapse duplicate-URL entries (e.g. from concurrent local + CI
        crawls both appending the same video/article before either saves).
        Keeps whichever duplicate has richer data — a transcript beats none,
        more captions beats fewer."""
        best = {}
        for r in self.data.get("reviews", []):
            url = r.get("url")
            if not url:
                continue
            existing = best.get(url)
            if existing is None:
                best[url] = r
                continue
            r_richer = (
                (bool(r.get("transcript_available")), len(r.get("captions", []))) >
                (bool(existing.get("transcript_available")), len(existing.get("captions", [])))
            )
            if r_richer:
                best[url] = r
        deduped = list(best.values())

        # Language duplicates: bilingual channels (e.g. Jayden Wong) upload the
        # same review twice — an English and a non-English version — usually
        # within minutes of each other. When a same-channel English-titled video
        # exists within 12 h of a non-Latin-titled one, keep the English one.
        def _exact_ts(r):
            # Real publish timestamp only — the year-based fallback in
            # _published_ts would make all undated same-year videos "simultaneous".
            try:
                return datetime.fromisoformat((r.get("published_at") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None

        latin_ts_by_channel = {}
        for r in deduped:
            if r.get("media_type") != "video" or _is_nonlatin_title(r.get("title", "")):
                continue
            ts = _exact_ts(r)
            if ts:
                latin_ts_by_channel.setdefault((r.get("channel_title") or "").strip().lower(), []).append(ts)

        def _is_lang_dup(r):
            if r.get("media_type") != "video" or not _is_nonlatin_title(r.get("title", "")):
                return False
            ts = _exact_ts(r)
            if ts is None:
                return False
            partners = latin_ts_by_channel.get((r.get("channel_title") or "").strip().lower(), [])
            return any(abs(ts - p) <= 12 * 3600 for p in partners)

        deduped = [r for r in deduped if not _is_lang_dup(r)]

        removed = len(self.data.get("reviews", [])) - len(deduped)
        if removed > 0:
            print(f"[dedupe] removed {removed} duplicate review(s)")
        self.data["reviews"] = deduped

    def save_cache(self):
        self._dedupe_reviews()
        self.data["last_updated"] = datetime.now().isoformat()
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def reload_from_disk(self):
        """Re-read cache from disk (called before every API response)."""
        self.data = self.load_cache()
        self._backfill_fields()
        # Exclude Tier 3 sources — only known press (T1) and creators (T2)
        self.data["reviews"] = [
            r for r in self.data.get("reviews", [])
            if r.get("source_tier", 3) < 3
        ]

    def _backfill_fields(self):
        """Add default values for fields introduced after initial seed."""
        from ife_crawler import infer_ife_system, source_tier as compute_tier, is_official_promo
        for r in self.data.get("reviews", []):
            r.setdefault("ife_specs", {})
            r.setdefault("ife_system_inferred", False)
            if r.get("media_type") == "video":
                r["source_tier"] = 2
                # Always re-derive for videos so "YouTube" entries get corrected
                r["source_name"] = "Official" if is_official_promo(r.get("title", ""), r.get("channel_title", "")) else "Creator"
            elif "source_tier" not in r or r.get("source_name") in ("General", None, "YouTube"):
                t = compute_tier(r.get("url", ""))
                r["source_tier"] = t
                r["source_name"] = {1: "Press", 2: "Creator"}.get(t, "General")
            # Backfill start_seconds on captions that only have a "M:SS" string
            for cap in r.get("captions", []):
                if "start_seconds" not in cap and cap.get("timestamp"):
                    try:
                        parts = cap["timestamp"].split(":")
                        cap["start_seconds"] = int(parts[0]) * 60 + int(parts[1])
                    except Exception:
                        pass
            # Sentiment
            if "sentiment" not in r:
                label, score = _compute_sentiment(r)
                r["sentiment"] = label
                r["sentiment_score"] = score
            # Records with no detected system keep the airline-based inference
            # as a *guess* only — never shown as the system tag. Uncertain
            # systems stay untagged; manual tags come via /api/review-system.
            if not r.get("ife_system") and "ife_system_guess" not in r:
                r["ife_system_guess"] = infer_ife_system(
                    r.get("airlines_mentioned", []),
                    r.get("aircraft_mentioned", [])
                )

    def crawl_and_cache(self, airline=None, aircraft=None):
        crawler = IFECrawler(verify_ssl=False)
        results = crawler.crawl_review_sites(airline=airline, aircraft=aircraft)
        self.data["reviews"].extend(results)
        self.save_cache()
        return results

    def get_filter_options(self):
        """Return all distinct values for each filterable field."""
        airlines = set()
        aircraft = set()
        ife_systems = set()
        ife_features = set()
        years = set()
        media_types = set()
        transcript_options = {"Yes", "No"}

        for r in self.data.get("reviews", []):
            for a in r.get("airlines_mentioned", []):
                airlines.add(a["keyword"])
            for ac in r.get("aircraft_mentioned", []):
                aircraft.add(ac["keyword"])
            if r.get("ife_system"):
                ife_systems.add(r["ife_system"])
            for f in r.get("ife_features", {}).keys():
                ife_features.add(f)
            if r.get("year"):
                years.add(r["year"])
            if r.get("media_type"):
                media_types.add(r["media_type"])

        source_names = set()
        for r in self.data.get("reviews", []):
            name = r.get("source_name", "Creator")
            if name in ("Press", "Creator", "Official"):
                source_names.add(name)

        return {
            "years":        sorted(years, reverse=True),
            "airlines":     sorted(airlines),
            "ife_systems":  sorted(ife_systems),
            "ife_features": sorted(ife_features),
            "media_types":  sorted(media_types),
            "transcript":   sorted(transcript_options),
            "chapters":     ["Has chapters", "IFE chapter"],
            "source_tiers": sorted(source_names),
        }

    def _relevance(self, r):
        """Compute an IFE content density score (purely factual, not quality)."""
        score = 0.0
        # Transcript resources always rank above non-transcript (0.60 base guarantee)
        if r.get("transcript_available"):
            score += 0.60
        score += min(len(r.get("ife_features", {})) * 0.12, 0.24)
        if r.get("ife_system"):
            score += 0.12
        score += min(len(r.get("airlines_mentioned", [])) * 0.04, 0.08)
        return round(min(score, 1.0), 3)

    def _published_ts(self, r):
        """Sort key: publish time (newest first when reversed); falls back to year."""
        pa = r.get("published_at") or ""
        try:
            return datetime.fromisoformat(pa.replace("Z", "+00:00")).timestamp()
        except ValueError:
            # No publish date (some articles): treat as Jan 1 of its year so it
            # sorts within the right year but below any dated item from it.
            try:
                return datetime(int(r.get("year")), 1, 1).timestamp()
            except (ValueError, TypeError, OSError):
                return 0.0

    def filter_reviews(self, filters):
        results = self.data.get("reviews", [])

        if filters.get("airlines"):
            results = [r for r in results if any(
                a["keyword"] in filters["airlines"]
                for a in r.get("airlines_mentioned", [])
            )]

        if filters.get("aircraft"):
            results = [r for r in results if any(
                ac["keyword"] in filters["aircraft"]
                for ac in r.get("aircraft_mentioned", [])
            )]

        if filters.get("ife_systems"):
            results = [r for r in results if r.get("ife_system") in filters["ife_systems"]]

        if filters.get("ife_features"):
            results = [r for r in results if any(
                f in r.get("ife_features", {})
                for f in filters["ife_features"]
            )]

        if filters.get("years"):
            results = [r for r in results if r.get("year") in filters["years"]]

        if filters.get("media_types"):
            results = [r for r in results if r.get("media_type") in filters["media_types"]]

        if filters.get("transcript"):
            want = filters["transcript"]
            if "Yes" in want and "No" not in want:
                results = [r for r in results if r.get("transcript_available")]
            elif "No" in want and "Yes" not in want:
                results = [r for r in results if not r.get("transcript_available")]

        if filters.get("chapters"):
            want = set(filters["chapters"])
            if "IFE chapter" in want:
                results = [r for r in results
                           if any(c.get("ife") for c in (r.get("chapters") or []))]
            elif "Has chapters" in want:
                results = [r for r in results if r.get("chapters")]

        if filters.get("source_tiers"):
            wanted = set(filters["source_tiers"])
            if wanted:
                results = [r for r in results if r.get("source_name", "Creator") in wanted]

        # Always sort newest first
        results.sort(key=self._published_ts, reverse=True)
        return results

    def paginate(self, rows, page, per_page=50):
        total = len(rows)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        return {
            "rows": rows[start:start + per_page],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }

    def get_summary(self, reviews=None):
        reviews = reviews if reviews is not None else self.data.get("reviews", [])
        if not reviews:
            return {}

        airlines = {}
        aircraft = {}
        ife_systems = {}
        features = {}
        media_types = {}
        by_year = {}
        sentiment = {"positive": 0, "neutral": 0, "negative": 0}
        with_transcript = 0

        for r in reviews:
            for a in r.get("airlines_mentioned", []):
                airlines[a["keyword"]] = airlines.get(a["keyword"], 0) + a["mentions"]
            for ac in r.get("aircraft_mentioned", []):
                aircraft[ac["keyword"]] = aircraft.get(ac["keyword"], 0) + ac["mentions"]
            sys = r.get("ife_system", "Unknown")
            ife_systems[sys] = ife_systems.get(sys, 0) + 1
            for f in r.get("ife_features", {}).keys():
                features[f] = features.get(f, 0) + 1
            mt = r.get("media_type", "unknown")
            media_types[mt] = media_types.get(mt, 0) + 1
            yr = r.get("year")
            if yr:
                by_year[str(yr)] = by_year.get(str(yr), 0) + 1
            sent = r.get("sentiment", "neutral")
            if sent in sentiment:
                sentiment[sent] += 1
            if r.get("transcript_available"):
                with_transcript += 1

        return {
            "total_reviews":    len(reviews),
            "with_transcript":  with_transcript,
            "top_airlines":     sorted(airlines.items(), key=lambda x: x[1], reverse=True)[:5],
            "top_aircraft":     sorted(aircraft.items(), key=lambda x: x[1], reverse=True)[:5],
            "ife_systems":      sorted(ife_systems.items(), key=lambda x: x[1], reverse=True),
            "ife_features":     sorted(features.items(), key=lambda x: x[1], reverse=True),
            "media_types":      media_types,
            "by_year":          sorted(by_year.items()),
            "sentiment":        sentiment,
        }

    def clear_cache(self):
        self.data = {"reviews": [], "last_updated": None}
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
