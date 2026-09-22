@echo off
rem Manual/scheduled local discovery crawl. Runs from this file's directory.
cd /d "%~dp0"
set PY=python
if defined IFE_PYTHON set PY=%IFE_PYTHON%
"%PY%" daily_crawl.py >> crawl_log.txt 2>&1
