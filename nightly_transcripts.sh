#!/usr/bin/env bash
export PATH="$HOME/.local/bin:$PATH"   # user-local git-lfs for the cache
# Nightly transcript backfill + translation on the VM, then commit and push
# the enriched cache so the cloud crawl (03:00 UTC) and this box stop
# diverging. Scheduled at 06:00 UTC from the user crontab; the cloud chapter
# job runs at 13:30 UTC; the night is budgeted to end before it: OCR of new videos
# 2 h, OCR backlog up to 3 h, Whisper 2 h (each is a cap and ends early when done).
#
# Needs YOUTUBE_COOKIES_B64 in .env, or YouTube bot-blocks every fetch; if it
# is absent the run is a no-op so it does not burn CPU for nothing.
set -o pipefail
cd "$(dirname "$0")" || exit 1
LOG=nightly_transcripts_log.txt
PY=.venv/bin/python
export GIT_TERMINAL_PROMPT=0

log() { echo "$(date -u '+%F %T') $*" >> "$LOG"; }

log "==== nightly run started ===="

# Start from the latest upstream cache (the cloud crawl committed at 03:00).
# --autostash carries local notes/flags edits across; if the rebase cannot
# apply cleanly we keep working on the local state and sort it out at push.
if ! git pull --rebase --autostash origin main >> "$LOG" 2>&1; then
    log "pull --rebase failed; continuing with local state"
    git rebase --abort >> "$LOG" 2>&1
fi

# The ios-screen-recording-ocr checkout (~/ife) and its venv (~/ife-venv: torch-cpu,
# open_clip, rapidocr, yt-dlp); every OCR step below is skipped when absent.
OCR_REPO="${IFE_OCR_REPO:-$HOME/ife}"
OCR_PY="${IFE_OCR_PY:-$HOME/ife-venv/bin/python}"

# Discovery backfill (2024 -> now): known reviewer channels' upload lists plus
# quarter-sliced searches, within a fixed API budget per night so the cloud
# crawl's own quota is not starved. Resumes from backfill_discovery_state.json.
# Same Google project as the cloud crawl (which needs ~9k of the 10k/day), so
# only the cheap channel pass runs nightly; the search pass waits for a quota
# increase: set BACKFILL_SEARCH=1 (and a bigger BACKFILL_UNITS) once granted.
if [ "${BACKFILL_SEARCH:-0}" = "1" ]; then
    BACKFILL_UNITS="${BACKFILL_UNITS:-3000}" "$PY" -u backfill_discovery.py >> "$LOG" 2>&1
else
    BACKFILL_UNITS="${BACKFILL_UNITS:-600}" "$PY" -u backfill_discovery.py --channels-only >> "$LOG" 2>&1
fi

# A video without chapters and tags is of no use to the dashboard, and every new video
# is held (hidden) until it has both, so OCR is the critical path of the night and runs
# before the transcript grind. Discovery goes first so what it adds is read tonight.
# New videos first: everything the crawl or discovery added that no OCR pass has read yet
# (Short, chaptered or long; videos still held at admission whatever their publish date),
# on two shards in parallel (4 cores), ahead of the backlog passes.
if [ -x "$OCR_PY" ] && [ -f "$OCR_REPO/ocr_video_chapters.py" ] && "$OCR_PY" -c "import yt_dlp" 2>/dev/null         && grep -qE '^YOUTUBE_COOKIES_B64=.+' .env 2>/dev/null; then
    IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="${NEW_OCR_MIN:-120}" "$OCR_PY" -u gather_short_chapters.py --new --interval "${NEW_OCR_INTERVAL:-4}" --shard 0/2 >> "$LOG" 2>&1 &
    IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="${NEW_OCR_MIN:-120}" "$OCR_PY" -u gather_short_chapters.py --new --interval "${NEW_OCR_INTERVAL:-4}" --shard 1/2 >> "$LOG" 2>&1 &
    wait
fi

# OCR screen tagging. Uses the ios-screen-recording-ocr checkout (~/ife) and
# its venv (~/ife-venv: torch-cpu, open_clip, rapidocr); skipped when absent.
#  - internal reviews' own photos/recordings -> tags, verified tags, chapters
#    (resumable: only files without detections are processed. The dashboard
#    already runs this on every upload/import (app.py _trigger_ocr); tonight's
#    run is the safety net for anything that trigger missed)
#  - Shorts without description chapters -> OCR chapters, 60 min cap
if [ -x "$OCR_PY" ] && [ -f "$OCR_REPO/ocr_video_chapters.py" ]; then
    IFE_OCR_REPO="$OCR_REPO" "$OCR_PY" -u tag_internal_media.py >> "$LOG" 2>&1
    if "$OCR_PY" -c "import yt_dlp" 2>/dev/null; then
        IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="${SHORTS_OCR_MIN:-60}" "$OCR_PY" -u gather_short_chapters.py >> "$LOG" 2>&1
        # every video with a description chapter flagged IFE: OCR just that chapter
        # (ranged download) and keep its screen-tag detections, newest first, 90 min cap
        # Two shards in parallel (4 cores); 4 s frames (6 s missed a screen held ~10 s); a screen held two frames is the bar.
        IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="${IFE_CHAPTER_OCR_MIN:-90}" "$OCR_PY" -u gather_short_chapters.py --ife-chapters --interval "${IFE_OCR_INTERVAL:-4}" --shard 0/2 >> "$LOG" 2>&1 &
        IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="${IFE_CHAPTER_OCR_MIN:-90}" "$OCR_PY" -u gather_short_chapters.py --ife-chapters --interval "${IFE_OCR_INTERVAL:-4}" --shard 1/2 >> "$LOG" 2>&1 &
        wait
        # external full-length reviews without chapters, best IFE candidates first, 90 min cap
        # (transcripts now open IFE chapters for most of these, which the targeted pass covers)
        # (the Shorts pass finishes in seconds now that every Short is done, so its hour moved here).
        # 4 s frame interval: ~4x faster than 2 s with the same tags and ~1 chapter fewer per video.
        IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="${LONG_OCR_MIN:-90}" "$OCR_PY" -u gather_short_chapters.py --long --interval "${LONG_OCR_INTERVAL:-4}" >> "$LOG" 2>&1
    else
        log "yt_dlp missing in $OCR_PY - skipping Shorts OCR chapters"
    fi
else
    log "OCR repo/venv not found ($OCR_REPO, $OCR_PY) - skipping screen tagging"
fi

# Transcripts matter less than chapters and tags (a held video no longer waits on one), so
# Whisper gets what is left of the night.
if grep -qE '^YOUTUBE_COOKIES_B64=.+' .env 2>/dev/null; then
    MAX_RUNTIME_MIN="${MAX_RUNTIME_MIN:-120}" "$PY" -u backfill_transcripts.py >> "$LOG" 2>&1
    "$PY" -u translate_captions.py >> "$LOG" 2>&1
else
    log "no YOUTUBE_COOKIES_B64 in .env - skipping transcript grind"
fi

# Whisper over the internal reviews' own recordings (dashboard venv has whisper).
"$PY" -u transcribe_internal.py >> "$LOG" 2>&1

# Drop entries the gate rejects today and videos the OCR pass has sampled
# without finding an IFE screen (copies kept in pruned_offtopic.json).
"$PY" prune_offtopic.py --apply >> "$LOG" 2>&1

# Publish: cache + team notes/bookmarks + uploaded photos. Retry once after a
# rebase in case CI or a teammate pushed while Whisper was running.
git add ife_cache.json notes.json flags.json pruned_offtopic.json >> "$LOG" 2>&1
[ -d static/uploads ] && git add static/uploads >> "$LOG" 2>&1
if git diff --staged --quiet; then
    log "nothing to commit"
else
    git commit -q -m "data: nightly transcripts + OCR screen tags (VM)" >> "$LOG" 2>&1
    if ! git push origin main >> "$LOG" 2>&1; then
        log "push rejected; rebasing onto origin/main and retrying"
        if ! git pull --rebase --autostash origin main >> "$LOG" 2>&1; then
            # Two writers to one JSON (cloud crawl + this box): merge the cache by
            # URL instead of giving up, then continue the rebase.
            if "$PY" resolve_cache_conflict.py >> "$LOG" 2>&1 && GIT_EDITOR=true git rebase --continue >> "$LOG" 2>&1; then
                log "cache conflict merged by URL"
            else
                git rebase --abort >> "$LOG" 2>&1
                log "PUSH FAILED - local commit kept; resolve manually"
            fi
        fi
        if git push origin main >> "$LOG" 2>&1; then log "push succeeded after rebase"; fi
    fi
fi

log "==== nightly run finished ===="
