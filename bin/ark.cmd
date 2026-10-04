@echo off
REM Double-clickable front door for bin/ark.py, and deliberately the whole of
REM it. Every argument is passed straight through, so `ark.cmd up --no-models`
REM works exactly like the python call. RUNBOOK.md remains the source of truth
REM for what each server is and why; this file starts nothing on its own.
REM
REM With no arguments it reports status and pauses, because a window that
REM opens, prints, and vanishes is the same as no window at all.
python "%~dp0ark.py" %*
if "%~1"=="" pause
