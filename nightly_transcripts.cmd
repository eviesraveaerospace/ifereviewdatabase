@echo off
rem Nightly transcript backfill window: grind Whisper transcripts for videos
rem that lack them, translate any non-English transcripts, then commit and
rem push the gains. Progress is checkpointed, so an interrupted run loses at
rem most ~25 videos of work.
rem
rem Used by the "IFE ReviewDB Nightly Transcripts" scheduled task (9 PM daily).
rem Portable: runs from whatever directory this file lives in, uses `python`
rem from PATH (override with IFE_PYTHON=C:\path\to\python.exe), and pushes to
rem `origin`. Register the task with:
rem   schtasks /Create /TN "IFE ReviewDB Nightly Transcripts" /SC DAILY /ST 21:00 ^
rem     /TR "\"%CD%\nightly_transcripts.cmd\"" /F
rem
rem Requirements on the host: git, ffmpeg on PATH, pip install -r requirements.txt,
rem and a .env with YOUTUBE_COOKIES_B64 (or a signed-in Edge/Chrome/Firefox
rem profile) so YouTube does not bot-block the audio downloads.

setlocal
cd /d "%~dp0"

set LOG=nightly_transcripts_log.txt
set PY=python
if defined IFE_PYTHON set PY=%IFE_PYTHON%
if not defined MAX_RUNTIME_MIN set MAX_RUNTIME_MIN=300

echo ==== nightly run started %date% %time% ==== >> %LOG%

rem Start from the latest cloud crawl so tonight's commit does not diverge from CI.
git pull --rebase --autostash origin main >> %LOG% 2>&1
if errorlevel 1 (
    echo WARNING: pull failed; continuing on the local copy >> %LOG%
    git rebase --abort >> %LOG% 2>&1
)

"%PY%" -u backfill_transcripts.py >> %LOG% 2>&1
"%PY%" -u translate_captions.py >> %LOG% 2>&1

git add ife_cache.json notes.json flags.json >> %LOG% 2>&1
if exist static\uploads git add static\uploads >> %LOG% 2>&1
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "data: nightly transcript backfill" >> %LOG% 2>&1
    git push origin main >> %LOG% 2>&1
    if errorlevel 1 (
        rem CI committed while we were grinding: replay our commit on top and retry.
        git pull --rebase origin main >> %LOG% 2>&1
        if errorlevel 1 (
            echo WARNING: rebase conflict; commit left local, resolve by hand >> %LOG%
            git rebase --abort >> %LOG% 2>&1
        ) else (
            git push origin main >> %LOG% 2>&1
        )
    )
) else (
    echo no changes to commit >> %LOG%
)

echo ==== nightly run finished %date% %time% ==== >> %LOG%
endlocal
exit /b 0
