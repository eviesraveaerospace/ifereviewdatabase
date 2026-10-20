@echo off
rem Nightly caption fetch from the laptop: YouTube serves caption tracks to
rem this egress (the VM is blocked), so this box pulls YouTube captions only,
rem no Whisper, for the videos most likely to be IFE reviews, then commits and
rem pushes. The VM's Whisper night then takes only videos marked caption-less.
rem Progress is checkpointed, so an interrupted run loses at most ~25 videos.
rem
rem Used by the "IFE ReviewDB Nightly Captions" scheduled task (9 PM daily).
rem Portable: runs from whatever directory this file lives in, uses `python`
rem from PATH (override with IFE_PYTHON=C:\path\to\python.exe), and pushes to
rem `team`. Register the task with:
rem   schtasks /Create /TN "IFE ReviewDB Nightly Captions" /SC DAILY /ST 21:00 ^
rem     /TR "\"%CD%\nightly_transcripts.cmd\"" /F
rem
rem Requirements on the host: git + git-lfs on PATH, pip install -r requirements.txt.

setlocal
cd /d "%~dp0"

set LOG=nightly_transcripts_log.txt
set PY=python
if defined IFE_PYTHON set PY=%IFE_PYTHON%
if not defined MAX_RUNTIME_MIN set MAX_RUNTIME_MIN=150
set CAPTIONS_ONLY=1

echo ==== nightly run started %date% %time% ==== >> %LOG%

rem Start from the latest cloud crawl so tonight's commit does not diverge from CI.
git pull --rebase --autostash team main >> %LOG% 2>&1
if errorlevel 1 (
    echo WARNING: pull failed; continuing on the local copy >> %LOG%
    git rebase --abort >> %LOG% 2>&1
)

"%PY%" -u backfill_transcripts.py >> %LOG% 2>&1

git add ife_cache.json notes.json flags.json >> %LOG% 2>&1
if exist static\uploads git add static\uploads >> %LOG% 2>&1
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "data: nightly caption fetch (laptop)" >> %LOG% 2>&1
    git push team main >> %LOG% 2>&1
    if errorlevel 1 (
        rem CI or the VM committed meanwhile: replay our commit on top, merging the cache by URL.
        git pull --rebase --autostash team main >> %LOG% 2>&1
        if errorlevel 1 (
            "%PY%" resolve_cache_conflict.py >> %LOG% 2>&1
            set GIT_EDITOR=true
            git rebase --continue >> %LOG% 2>&1
            if errorlevel 1 (
                echo WARNING: rebase conflict; commit left local, resolve by hand >> %LOG%
                git rebase --abort >> %LOG% 2>&1
            )
        )
        git push team main >> %LOG% 2>&1
    )
) else (
    echo no changes to commit >> %LOG%
)

echo ==== nightly run finished %date% %time% ==== >> %LOG%
endlocal
exit /b 0
