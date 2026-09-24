#!/usr/bin/env bash
# Nightly transcript backfill + translation on the VM, then commit and push
# the enriched cache so the cloud crawl (03:00 UTC) and this box stop
# diverging. Scheduled at 06:00 UTC from the user crontab; the cloud chapter
# job runs at 13:30 UTC, after the 5 h Whisper window has ended.
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

if ! grep -qE '^YOUTUBE_COOKIES_B64=.+' .env 2>/dev/null; then
    log "no YOUTUBE_COOKIES_B64 in .env - skipping transcript grind"
    log "==== nightly run finished ===="
    exit 0
fi

# Start from the latest upstream cache (the cloud crawl committed at 03:00).
# --autostash carries local notes/flags edits across; if the rebase cannot
# apply cleanly we keep working on the local state and sort it out at push.
if ! git pull --rebase --autostash origin main >> "$LOG" 2>&1; then
    log "pull --rebase failed; continuing with local state"
    git rebase --abort >> "$LOG" 2>&1
fi

MAX_RUNTIME_MIN="${MAX_RUNTIME_MIN:-300}" "$PY" -u backfill_transcripts.py >> "$LOG" 2>&1
"$PY" -u translate_captions.py >> "$LOG" 2>&1

# Publish: cache + team notes/bookmarks + uploaded photos. Retry once after a
# rebase in case CI or a teammate pushed while Whisper was running.
git add ife_cache.json notes.json flags.json >> "$LOG" 2>&1
[ -d static/uploads ] && git add static/uploads >> "$LOG" 2>&1
if git diff --staged --quiet; then
    log "nothing to commit"
else
    git commit -q -m "data: nightly transcript backfill (VM)" >> "$LOG" 2>&1
    if ! git push origin main >> "$LOG" 2>&1; then
        log "push rejected; rebasing onto origin/main and retrying"
        if git pull --rebase origin main >> "$LOG" 2>&1 && git push origin main >> "$LOG" 2>&1; then
            log "push succeeded after rebase"
        else
            git rebase --abort >> "$LOG" 2>&1
            log "PUSH FAILED - local commit kept; resolve manually"
        fi
    fi
fi

log "==== nightly run finished ===="
