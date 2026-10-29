#!/usr/bin/env bash
export PATH="$HOME/.local/bin:$PATH"   # user-local git-lfs for the cache
# Nightly transcript backfill + translation on the VM, then commit and push
# the enriched cache so the cloud crawl (03:00 UTC) and this box stop
# diverging. Scheduled at 06:00 UTC from the user crontab; the cloud chapter
# job runs at 13:30 UTC, so the work is budgeted to end by NIGHT_DEADLINE_UTC (13:00):
# discovery, OCR of new videos, the OCR backlog, then Whisper with what is left (every
# phase is capped by the time remaining, and the log records each phase's real length).
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
# before the transcript grind. Discovery runs first so what it adds is read tonight.
#
# Time budget: everything below must be done by NIGHT_DEADLINE_UTC (the cloud chapter job
# pushes at 13:30 UTC). Each OCR phase gets min(its cap, minutes left to the deadline minus
# WHISPER_RESERVE_MIN kept for the transcript pass), the log records the budget and how
# long the phase really took, and a phase with no time left is logged as skipped - so a
# slow night shows up in the log instead of as a late push.
NIGHT_DEADLINE_UTC="${NIGHT_DEADLINE_UTC:-13:00}"
WHISPER_RESERVE_MIN="${WHISPER_RESERVE_MIN:-30}"

minutes_left() {    # minutes until the deadline (tomorrow's if today's has passed)
    local now end
    now=$(date -u +%s); end=$(date -u -d "$NIGHT_DEADLINE_UTC" +%s)
    [ "$end" -le "$now" ] && end=$((end + 86400))
    echo $(( (end - now) / 60 ))
}

budget() {          # budget CAP [RESERVE]: minutes a phase may run; 0 = no time left, skip it
    local left cap=$1
    left=$(( $(minutes_left) - ${2:-0} ))
    if [ "$left" -lt 5 ]; then echo 0; elif [ "$left" -lt "$cap" ]; then echo "$left"; else echo "$cap"; fi
}

ocr_pass() {        # ocr_pass NAME CAP SHARDS [gather_short_chapters.py args...]
    local name=$1 cap=$2 shards=$3 b k t0=$SECONDS
    shift 3
    b=$(budget "$cap" "$WHISPER_RESERVE_MIN")
    if [ "$b" -lt 1 ]; then log "OCR $name: skipped, no time left before $NIGHT_DEADLINE_UTC"; return; fi
    for ((k = 0; k < shards; k++)); do
        IFE_OCR_REPO="$OCR_REPO" MAX_RUNTIME_MIN="$b" "$OCR_PY" -u gather_short_chapters.py "$@" --shard "$k/$shards" >> "$LOG" 2>&1 &
    done
    wait
    log "OCR $name: budget ${b} min, took $(( (SECONDS - t0) / 60 )) min"
}

# Results other machines computed are applied first, so the passes below skip those videos:
# the laptop's (laptop_ocr.ps1 pushes ocr_results/laptop.json) and the cloud job's (the
# ios-screen-recording-ocr repo's Actions workflow commits cloud/ocr_results/cloud.json there).
apply_ocr_results() {
    git -C "$OCR_REPO" pull --ff-only -q >> "$LOG" 2>&1 || log "could not pull $OCR_REPO: cloud OCR results may be stale"
    "$PY" ocr_results.py ocr_results/*.json "$OCR_REPO"/cloud/ocr_results/*.json >> "$LOG" 2>&1
}
apply_ocr_results

log "time budget: $(minutes_left) min until $NIGHT_DEADLINE_UTC UTC"
OCR_READY=0
if [ -x "$OCR_PY" ] && [ -f "$OCR_REPO/ocr_video_chapters.py" ] && "$OCR_PY" -c "import yt_dlp" 2>/dev/null; then
    OCR_READY=1
fi

# New videos first: everything the crawl or discovery added that no OCR pass has read yet
# (Short, chaptered or long; videos still held at admission whatever their publish date),
# best IFE-review candidates first so what waits when the budget runs out is the least likely
# to matter, on two shards in parallel (4 cores). The cap is generous: the pass ends as
# soon as the queue is empty.
if [ "$OCR_READY" = 1 ] && grep -qE '^YOUTUBE_COOKIES_B64=.+' .env 2>/dev/null; then
    ocr_pass "new videos" "${NEW_OCR_MIN:-240}" 2 --new --interval "${NEW_OCR_INTERVAL:-4}"
fi

# OCR screen tagging backlog. Uses the ios-screen-recording-ocr checkout (~/ife) and
# its venv (~/ife-venv: torch-cpu, open_clip, rapidocr); skipped when absent.
#  - internal reviews' own photos/recordings -> tags, verified tags, chapters
#    (resumable: only files without detections are processed. The dashboard
#    already runs this on every upload/import (app.py _trigger_ocr); tonight's
#    run is the safety net for anything that trigger missed)
#  - Shorts without description chapters -> OCR chapters
#  - every video with a description chapter flagged IFE: OCR just that chapter (ranged
#    download) and keep its screen-tag detections, newest first
#  - chaptered videos with no IFE-looking chapter and no tags: whole-video OCR for the tags
#    (the description chapters are kept)
#  - external full-length reviews without chapters, best IFE candidates first
# Two shards in parallel (4 cores); 4 s frames (6 s missed a screen held ~10 s, and 4 s is
# ~4x faster than 2 s with the same tags); a screen held two frames is the bar.
if [ "$OCR_READY" = 1 ]; then
    IFE_OCR_REPO="$OCR_REPO" "$OCR_PY" -u tag_internal_media.py >> "$LOG" 2>&1
    ocr_pass "Shorts" "${SHORTS_OCR_MIN:-60}" 2
    ocr_pass "IFE chapters" "${IFE_CHAPTER_OCR_MIN:-90}" 2 --ife-chapters --interval "${IFE_OCR_INTERVAL:-4}"
    ocr_pass "chaptered videos without tags" "${TAG_CHAPTERED_OCR_MIN:-90}" 2 --tag-chaptered --interval "${TAG_CHAPTERED_OCR_INTERVAL:-4}"
    ocr_pass "full-length videos without chapters" "${LONG_OCR_MIN:-90}" 2 --long --interval "${LONG_OCR_INTERVAL:-4}"
else
    log "OCR repo/venv not found or yt_dlp missing ($OCR_REPO, $OCR_PY) - skipping screen tagging"
fi

# Transcripts matter less than chapters and tags (a held video no longer waits on one), so
# Whisper gets what is left of the night, and WHISPER_RESERVE_MIN of it is always kept.
if grep -qE '^YOUTUBE_COOKIES_B64=.+' .env 2>/dev/null; then
    WB=$(budget "${MAX_RUNTIME_MIN:-120}")
    if [ "$WB" -ge 1 ]; then
        t0=$SECONDS
        MAX_RUNTIME_MIN="$WB" "$PY" -u backfill_transcripts.py >> "$LOG" 2>&1
        "$PY" -u translate_captions.py >> "$LOG" 2>&1
        log "Whisper: budget ${WB} min, took $(( (SECONDS - t0) / 60 )) min"
    else
        log "Whisper: skipped, no time left before $NIGHT_DEADLINE_UTC"
    fi
else
    log "no YOUTUBE_COOKIES_B64 in .env - skipping transcript grind"
fi

# Whisper over the internal reviews' own recordings (dashboard venv has whisper).
"$PY" -u transcribe_internal.py >> "$LOG" 2>&1

apply_ocr_results

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
