# IFE Review Database — System Guide

Everything about how this system works: what it collects, how it parses, what queries and keywords it uses, when things run, and how to pick the work back up on a fresh machine.

Last updated: July 31, 2026 · Database at time of writing: **1,249 reviews** (1,225 videos, 24 articles), **622 with transcripts**, **107 explicitly matched to a named IFE system**. Non-English titles, captions, and comments carry English translations (`title_en` / `text_en`).

---

## 1. What this is

A self-hosted competitive-intelligence tool for airline in-flight entertainment (IFE). It crawls public YouTube reviews (plus some press articles), tags each one with the airlines, aircraft, IFE system, and IFE features it discusses, and serves an interactive dashboard for browsing, filtering, comparing, and annotating.

- **Dashboard:** Flask app ([app.py](app.py)) + single-page UI ([templates/index.html](templates/index.html))
- **Launcher:** `python serve.py` → binds `0.0.0.0:5000` so teammates on the office network can open `http://<your-ip>:5000/`
- **Repos:** https://github.com/eviesraveaerospace/ifereviewdatabase (primary; runs the daily GitHub Actions crawl) · https://github.com/eviebngo/IFE-Review-Crawler (original, retired Sep 2026)

## 2. Where the data lives

| File | Contents |
|---|---|
| `ife_cache.json` | The database. One JSON object with a `reviews` list — every crawled video/article with all metadata, tags, and transcripts. Committed to git; CI also commits to it daily. |
| `notes.json` | Team notes on videos, keyed by video URL (server-side, shared). |
| `flags.json` | Saved/bookmarked videos (server-side, shared — migrated from browser localStorage July 30). |
| `channel_stats.json` | Known reviewer channels: subscribers, video counts, thumbnails. Refreshed by `gather_channel_stats.py`. |
| `.query_offset` | Rotation cursor so each crawl uses a different slice of the generated query list. Local-only. |
| `.env` (not in git) | `YOUTUBE_API_KEY` (crawling) and `ANTHROPIC_API_KEY` (Ask AI chat). |

### Fields on each review record

`url`, `title`, `year`, `published_at`, `channel_title`, `view_count`, `like_count`, `media_type` (video/article), `source_tier`/`source_name` (1 Press, 2 Creator/Official), `airlines_mentioned`, `aircraft_mentioned`, `ife_system` (only when explicitly named in content), `ife_system_guess` (airline-based inference — never displayed), `ife_system_manual` (true when hand-corrected), `ife_features` (feature tags), `ife_specs`, `sentiment`, `transcript_available`, `transcript_excerpt`, `captions` (timestamped lines), `transcript_full`, `transcript_source` (captions/whisper), `chapters`, `yt_comments`.

## 3. How data is collected and parsed

### Discovery (finding videos)

1. **YouTube search** (`search.list`) over the query list (section 4). Each query returns up to 10 video IDs.
2. **Metadata** via `videos.list`: title, channel, publish date, view/like counts.
3. **Articles**: DuckDuckGo search over 48 article queries plus a curated trusted-source list (Simple Flying etc.); pages fetched and text-extracted with BeautifulSoup.
4. Already-seen URLs are skipped; results append to `ife_cache.json` (deduped by URL on save, richer record wins).

### Parsing / tagging (per item)

The searchable text = title + description + transcript. Against it:

- **Airlines & aircraft**: keyword lists → `airlines_mentioned` / `aircraft_mentioned` with mention counts.
- **IFE system**: `IFE_SYSTEM_PATTERNS` (Panasonic eX1/eX2/eX3/Astrova, Thales AVANT/AVANT Up, Safran RAVE/RAVE Ultra, Collins Venue, KrisWorld, Oryx One, Emirates ICE, StudioCX, Gogo Avance, Viasat, and more). **A system is tagged only when explicitly named.** If not named, an airline-based inference is stored as `ife_system_guess` and never shown — this rule was added July 30 (941 old inferred tags were demoted to guesses).
- **Features**: `IFE_FEATURE_KEYWORDS` (section 4) → `ife_features`.
- **Transcripts**: `youtube-transcript-api` (manual captions → auto-generated → any language). Caption lines are scored for IFE relevance; the best become `captions` (timestamped "IFE moments") and `transcript_excerpt`.
- **Specs/sentiment**: screen sizes, resolutions etc. into `ife_specs`; simple sentiment label.

### Transcript backfill (for videos discovery missed)

`backfill_transcripts.py` walks all videos without transcripts: YouTube captions first, then **local Whisper** (via yt-dlp audio download) for videos with no captions. Checkpoints the cache every 25 videos. YouTube bot-blocks anonymous transcript requests after a while — set `YOUTUBE_COOKIES_B64` (base64 of a Netscape cookies.txt exported from a logged-in browser) to get past it. **As of July 30: 539 videos still lack transcripts because of a bot-block mid-run; rerun with cookies to continue.**

## 4. Current queries and keywords

Counts as of July 30, 2026 (all defined in [ife_crawler.py](ife_crawler.py)):

- **357 YouTube search queries** total:
  - 76 curated (`YOUTUBE_QUERIES`): named systems ("Panasonic Astrova inflight entertainment review"), vendors, generic IFE review phrasings
  - 281 generated: airline names × query templates, plus aircraft types (A350-1000, 787-9, 777X, A220…)
- **Per-crawl budget: 85 queries** (`QUERY_BUDGET`) to stay inside API quota — curated queries get priority, at least 30 slots (`GENERATED_MIN`) go to generated queries, which **rotate** between runs via `.query_offset`, so successive crawls cover different airlines/aircraft.
- **48 article queries** (`AUTO_DISCOVERY_QUERIES`) for DuckDuckGo/press discovery.
- **13 feature keyword groups, 86 keywords** (`IFE_FEATURE_KEYWORDS`): entertainment_system, content, connectivity, 4k_display, quality, seat, usb_power, bluetooth_audio, and — added July 30 — **watch_party, seat_chat, search, tail_camera, moving_map**.
- Comment relevance filter (`_IFE_COMMENT_TERMS` in app.py) with false-positive exclusions ("headphone hook" etc.).

## 5. When things run

| What | When | Where |
|---|---|---|
| Daily discovery crawl | 3:00 AM UTC daily (`daily_crawl.yml`), commits `ife_cache.json` to git | GitHub Actions (cloud) |
| Transcript backfill workflows | manual dispatch (`backfill.yml`, `backfill_transcripts.yml`) | GitHub Actions |
| Local background crawl | on server start, then every 24 h while `serve.py`/`app.py` runs (7-day lookback, max 500) | your machine |
| Dashboard auto-start | Windows scheduled task "IFE ReviewDB Dashboard" launches `serve.py` (hidden) at every logon | your machine |
| Nightly transcript grind | Windows scheduled task "IFE ReviewDB Nightly Transcripts" — 9 PM daily, 5 h Whisper window via `nightly_transcripts.cmd`, then auto-commits and pushes gains | your machine |
| Daily chapter gather | 13:30 UTC (6:30 AM PT) daily (`daily_chapters.yml`), commits chapters to git | GitHub Actions (cloud) |
| Manual crawl | "Crawl" button in the dashboard → `/api/ife-seed` (365-day lookback) | your machine |
| Seed crawl | automatic on start only if the database has < 50 reviews | your machine |

The cloud and local crawls are independent — they sync only through git (`git pull` to receive CI's data, `git push` to publish local work). Dedupe-by-URL resolves overlaps.

**Cloud requirement:** the GitHub repo needs Actions secrets `YOUTUBE_API_KEY` and (for transcript workflows) `YOUTUBE_COOKIES_B64`. When moving to the team repo, re-add these under Settings → Secrets and variables → Actions.

## 6. The dashboard

Tabs: **Dashboard** (overview, popular channels, popular-features bar chart with per-feature breakdown modals, comments), **Reviews** (search + 8 facet filters, filter-aware CSV export), **Statistics** (KPIs, trends, momentum, coverage gaps, clickable rows that drill into filtered Reviews), **Compare** (head-to-head system comparison), **Ask AI** (Claude-powered Q&A over the corpus; needs `ANTHROPIC_API_KEY`).

Team input lives in the dashboard: notes (shared), saved videos (shared), and **manual IFE-system tag editing** — the ✎ button in a video's modal, which only appears and only works from the machine hosting the server (`/api/review-system` rejects other devices).

Other routes: `/report` (printable intelligence digest), `/export.csv` (respects active filters), `/stats` (legacy).

## 7. How to resume where things left off

### On this machine
```
python serve.py        # dashboard up at http://<your-ip>:5000
```
Everything else (daily crawl thread) starts automatically with it.

### On a fresh machine
```
git clone https://github.com/eviesraveaerospace/ifereviewdatabase.git
cd ifereviewdatabase
pip install -r requirements.txt
# create .env with YOUTUBE_API_KEY=... and optionally ANTHROPIC_API_KEY=...
python serve.py
```
The clone includes the full parsed database — no re-crawling needed.

### Keeping data in sync
```
git pull    # before doing anything — CI commits daily at 3 AM UTC
git push    # after local crawls/edits, so the repo (and teammates) get them
```

### Unfinished work / next steps (as of July 30, 2026)
1. **603 videos still need transcripts** — as of July 31, YouTube fully IP-blocks anonymous transcript requests from the office machine (every attempt fails immediately). Rerun `python backfill_transcripts.py` only with `YOUTUBE_COOKIES_B64` set (base64 of a cookies.txt exported from a signed-in browser), or dispatch the `backfill_transcripts.yml` workflow on GitHub if its cookie secret is still valid. Stop the dashboard first or accept checkpoint clobber risk on concurrent manual edits.
2. **System tags are sparse on purpose** (107 explicit of 1,249) — use the dashboard's ✎ editor to confirm systems video-by-video; manual tags are protected from automation.
3. **Share link blocked for teammates** — machine-side firewall is verified fine; suspect VPN/subnet/AP-isolation between clients. Teammate diagnostic: `Test-NetConnection <host-ip> -Port 5000`. Long-term fix: internal VM (discussion with IT in progress).
4. **YouTube API compliance review** — final notice July 30; respond within 7 business days with `compliance_sample_report.html` (regenerate anytime with `python generate_compliance_report.py`; kept out of git deliberately).
5. **Team repo Actions secrets** not yet configured (see section 5).

### Maintenance scripts (all idempotent unless noted)
| Script | Purpose |
|---|---|
| `backfill_transcripts.py` | Fetch missing transcripts (captions → Whisper) |
| `retag_features.py` | Re-apply feature keywords to all cached reviews (add-only) |
| `clear_inferred_systems.py` | Demote airline-inferred system tags to guesses |
| `gather_channel_stats.py` / `backfill_channels.py` | Refresh reviewer channel info |
| `gather_chapters.py` / `gather_comments.py` | Enrich videos with chapters / public comments |
| `regather_captions.py` / `merge_transcripts.py` | Caption maintenance |
| `translate_captions.py` | Translate non-English titles, captions, and comments to English |
| `purge_spam.py` | Remove known-spam content (destructive — review before running) |
| `generate_compliance_report.py` | Build the YouTube API compliance sample report |
