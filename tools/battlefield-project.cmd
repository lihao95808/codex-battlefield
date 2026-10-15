@echo off
rem battlefield-project wrapper: lets you type the command instead of a full path.
rem
rem ASCII only on purpose: cmd.exe parses .cmd files using the OEM codepage, so
rem non-ASCII comments get garbled into commands (hit that one already).
rem
rem The repo root is derived from this file's own location, so nothing is hardcoded.
rem This .cmd lives in <repo>\tools\, and <repo>\tools is on the user PATH
rem (same approach as codex-account).
setlocal
set "BF_TOOLS=%~dp0"
for %%I in ("%BF_TOOLS%..") do set "BF_REPO=%%~fI"

if not exist "%BF_REPO%\scripts\battlefield_project.py" (
  echo [battlefield-project] script not found: %BF_REPO%\scripts\battlefield_project.py
  echo                      this wrapper must live in ^<repo^>\tools\
  exit /b 2
)

set "BF_PY=%BF_REPO%\api\.venv\Scripts\python.exe"
if not exist "%BF_PY%" set "BF_PY=python"

"%BF_PY%" "%BF_REPO%\scripts\battlefield_project.py" %*
exit /b %ERRORLEVEL%
