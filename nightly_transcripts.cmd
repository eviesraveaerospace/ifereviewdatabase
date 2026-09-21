@echo off
rem Nightly transcript backfill window: grind Whisper transcripts for videos
rem that lack them, then commit and push the gains. Progress is checkpointed,
rem so an interrupted run loses at most ~25 videos of work.
rem Used by the "IFE ReviewDB Nightly Transcripts" scheduled task (9 PM daily).
cd /d C:\workspace\simple_crawler
set MAX_RUNTIME_MIN=300
echo ==== nightly run started %date% %time% ==== >> nightly_transcripts_log.txt
"C:\Users\engo\AppData\Local\Microsoft\WindowsApps\python.exe" -u backfill_transcripts.py >> nightly_transcripts_log.txt 2>&1
"C:\Users\engo\AppData\Local\Microsoft\WindowsApps\python.exe" -u translate_captions.py >> nightly_transcripts_log.txt 2>&1
git add ife_cache.json notes.json flags.json static/uploads >> nightly_transcripts_log.txt 2>&1
git commit -m "data: nightly transcript backfill" >> nightly_transcripts_log.txt 2>&1
git push origin main >> nightly_transcripts_log.txt 2>&1
git push team main >> nightly_transcripts_log.txt 2>&1
echo ==== nightly run finished %date% %time% ==== >> nightly_transcripts_log.txt
exit /b 0
