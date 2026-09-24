# IFE Review Database — System Guide

Everything about how this system works: what it collects, how it parses, what queries and keywords it uses, when things run, and how to pick the work back up on a fresh machine.

Last updated: September 22, 2026 · Database at time of writing: **7,443 reviews** (7,400 videos, 42 articles, 1 internal), **1,321 with transcripts** (323 via Whisper), **192 explicitly matched to a named IFE system**, **2,195 with chapters**. Non-English transcripts carry a full English translation (`transcript_full_en`); non-English titles, captions, and comments carry `title_en` / `text_en`.

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

`url`, `title`, `year`, `published_at`, `channel_title`, `view_count`, `like_count`, `media_type` (video/article), `source_tier`/`source_name` (1 Press, 2 Creator/Official), `airlines_mentioned`, `aircraft_mentioned`, `ife_system` (only when explicitly named in content), `ife_system_guess` (airline-based inference — never displayed), `ife_system_manual` (true when hand-corrected), `ife_features` (feature tags), `ife_specs`, `sentiment`, `transcript_available`, `transcript_excerpt`, `captions` (timestamped lines), `transcript_full`, `transcript_source` (captions/whisper), `transcript_lang` (langdetect code, or `garbage` for Whisper hallucination output), `transcript_full_en` (English translation when the transcript is not English), `transcript_excerpt_orig` (pre-translation excerpt), `title_en`, `chapters`, `yt_comments`.

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

`backfill_transcripts.py` walks all videos without transcripts: YouTube captions first, then **local Whisper** (via yt-dlp audio download) for videos with no captions. Checkpoints the cache every 25 videos with a merge-based save, so it can run while the dashboard or `translate_captions.py` is also writing. `MAX_RUNTIME_MIN` stops it cleanly after a time budget. YouTube bot-blocks anonymous transcript requests from most fixed IPs — set `YOUTUBE_COOKIES_B64` in `.env` (base64 of a Netscape cookies.txt exported from a logged-in browser); if unset it tries to borrow cookies from a signed-in Edge/Chrome/Firefox profile on the same machine. **As of September 22: 6,079 videos lack transcripts** — the daily cloud crawl adds far more videos than the nightly Whisper window can transcribe, so the backlog grows unless the grind runs every night.

### Translation (for non-English transcripts)

`translate_captions.py` runs after the backfill. For each transcript that is not English it produces `transcript_full_en` (offline **Argos Translate** when a language pack is installed, otherwise Google Translate via `deep-translator`), rebuilds `transcript_excerpt` / `captions` from the English text, and re-runs system/feature/airline/aircraft detection on it. It also flags Whisper hallucination output (repeated filler on silent or music-only audio) as `transcript_lang=garbage` so it is excluded from search. A legacy per-line pass still translates non-Latin titles, caption lines, and comments (`title_en` / `text_en`). Dashboard search and feature counters include the English text.

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
| Dashboard auto-start | Windows scheduled task "IFE ReviewDB Dashboard" launches `serve.py` (hidden) at every logon | host machine / VM |
| Nightly transcript grind | Windows scheduled task "IFE ReviewDB Nightly Transcripts" — 9 PM daily via `nightly_transcripts.cmd`: `git pull`, 5 h Whisper window (`MAX_RUNTIME_MIN=300`), translation pass, then auto-commits and pushes to `origin` (rebases and retries if CI pushed meanwhile). Script is path-independent: runs from its own directory and uses `python` from PATH (`IFE_PYTHON` overrides). | host machine / VM |
| Daily chapter gather | 13:30 UTC (6:30 AM PT) daily (`daily_chapters.yml`), commits chapters to git | GitHub Actions (cloud) |
| Manual crawl | "Crawl" button in the dashboard → `/api/ife-seed` (365-day lookback) | your machine |
| Seed crawl | automatic on start only if the database has < 50 reviews | your machine |

The cloud and local crawls are independent — they sync only through git (`git pull` to receive CI's data, `git push` to publish local work). Dedupe-by-URL resolves overlaps.

**Cloud requirement:** the GitHub repo needs Actions secrets `YOUTUBE_API_KEY` and (for the manual `backfill_transcripts.yml` workflow) `YOUTUBE_COOKIES_B64`, under Settings → Secrets and variables → Actions. `YOUTUBE_API_KEY` is confirmed configured on eviesraveaerospace/ifereviewdatabase — the daily crawl has been committing successfully. `YOUTUBE_COOKIES_B64` is unverified there; the cloud transcript workflow last succeeded July 31, and cookies expire, so re-export before dispatching it.

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

### On a fresh machine / the VM
```
git clone https://github.com/eviesraveaerospace/ifereviewdatabase.git
cd ifereviewdatabase
pip install -r requirements.txt      # requirements-lite.txt if Whisper is not needed
copy .env.example .env               # fill in YOUTUBE_API_KEY, YOUTUBE_COOKIES_B64, optionally ANTHROPIC_API_KEY
python serve.py
```
The clone includes the full parsed database — no re-crawling needed.

For the nightly grind the host also needs **ffmpeg on PATH** (Whisper audio decoding), git credentials that can push to `origin`, and `add_firewall_rule.ps1` run once if teammates should reach the dashboard. Then register the two scheduled tasks from the repo directory:
```
schtasks /Create /TN "IFE ReviewDB Dashboard" /SC ONLOGON /TR "\"%CD%\serve.py\"" /F
schtasks /Create /TN "IFE ReviewDB Nightly Transcripts" /SC DAILY /ST 21:00 /TR "\"%CD%\nightly_transcripts.cmd\"" /F
```
(For the dashboard task, point `/TR` at `pythonw.exe serve.py` if you want it hidden.) Smoke-test the grind before trusting the schedule: `set MAX_RUNTIME_MIN=5` then run `nightly_transcripts.cmd` and read `nightly_transcripts_log.txt` — it should show `Using YOUTUBE_COOKIES_B64` (or local browser cookies), a few `YT-OK` / `WH-OK` lines, and a push.

### Keeping data in sync
```
git pull    # before doing anything — CI commits daily at 3 AM UTC
git push    # after local crawls/edits, so the repo (and teammates) get them
```

### Unfinished work / next steps (as of September 22, 2026)
1. **Move the nightly grind to the VM.** The office-machine scheduled tasks are retired; `nightly_transcripts.cmd` and `run_crawl.bat` are now path-independent. Follow "On a fresh machine / the VM" above, export fresh YouTube cookies into `.env`, and smoke-test with a short `MAX_RUNTIME_MIN` before enabling the 9 PM task. The last nightly commit landed September 10.
2. **6,079 videos still need transcripts** (of 7,400). The cloud crawl now adds hundreds of videos a day and cookies-free transcript fetches are IP-blocked, so only the nightly Whisper window with cookies makes progress (the last two 5 h runs, Sep 1 and Sep 10, gained 79 and 157 transcripts). Consider `WHISPER_MODEL=tiny` on a slow VM, or dispatching `backfill_transcripts.yml` in parallel once its cookie secret is refreshed.
3. **Translation coverage.** 86 non-English transcripts are translated; 34 are flagged `garbage`. Argos language packs are only used when installed on the host (`argospm install translate-<lang>_en`); otherwise Google Translate is used, which is rate-limited and occasionally fails mid-run — the pass is idempotent, just rerun `python translate_captions.py`.
4. **System tags are sparse on purpose** (192 explicit of 7,443, 2 manual) — use the dashboard's ✎ editor to confirm systems video-by-video; manual tags are protected from automation.
5. **Share link for teammates** — once the dashboard runs on the VM this replaces the office-machine link that VPN/AP isolation blocked. Teammate diagnostic if it still fails: `Test-NetConnection <vm-ip> -Port 5000`.
6. **`YOUTUBE_COOKIES_B64` Actions secret** on the primary repo is unverified (see section 5). `YOUTUBE_API_KEY` is confirmed working.
7. **Retired repo** eviebngo/IFE-Review-Crawler still exists; nothing pushes to it anymore. Archive it on GitHub when convenient so nobody clones a stale copy.

### Maintenance scripts (all idempotent unless noted)
| Script | Purpose |
|---|---|
| `backfill_transcripts.py` | Fetch missing transcripts (captions → Whisper) |
| `retag_features.py` | Re-apply feature keywords to all cached reviews (add-only) |
| `clear_inferred_systems.py` | Demote airline-inferred system tags to guesses |
| `gather_channel_stats.py` / `backfill_channels.py` | Refresh reviewer channel info |
| `gather_chapters.py` / `gather_comments.py` | Enrich videos with chapters / public comments |
| `regather_captions.py` / `merge_transcripts.py` | Caption maintenance |
| `translate_captions.py` | Translate non-English transcripts (full text, Argos → Google fallback), titles, captions, and comments to English; flag Whisper-noise transcripts |
| `purge_spam.py` | Remove known-spam content (destructive — review before running) |
| `generate_compliance_report.py` | Build the YouTube API compliance sample report |

## Discovery changes — September 24, 2026

- **Cloud crawl is the canonical discovery path.** The in-process crawl in `app.py` is now opt-in (`LOCAL_CRAWL=1`); it used to start on every gunicorn boot and each start burned ~8,500 of the key's 10,000 daily search units, starving the scheduled crawl. Symptom was invisible: API errors were swallowed as "no results".
- **API errors are surfaced.** `IFECrawler.api_error` / `quota_exhausted` / `api_calls`; the query loop stops when the quota is gone; `daily_crawl.py` prints `** YOUTUBE QUOTA EXHAUSTED **` in its log line and `/api/crawl` shows it under `crawl.error`.
- **Every curated query now runs.** With 90 curated queries and 55 curated slots, positions 56–90 never ran; the curated list now rotates too (`.query_offset_curated`), so each runs at least every other crawl.
- **Lookback 14 → 30 days** in `daily_crawl.py`. The workflow was skipped Aug 5–Sep 21 (repository guard pointed at the retired repo); a 60-day backfill was dispatched to recover that window.
- **Two writers, one JSON:** the VM nightly script commits/pushes, and on a cache conflict runs `resolve_cache_conflict.py` (union by URL, richer copy wins) and continues the rebase.
- Trusted channels: added Gabe Leigh.
