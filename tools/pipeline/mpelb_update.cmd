@echo off
rem ============================================================================
rem  mpelb updater - project-local bridge binary.
rem  Usage: mpelb_update.cmd --check | --update
rem  Prefers .venv python; falls back to py -3 / python for a fresh checkout.
rem  NOTE: keep this file ASCII-only with CRLF line endings.
rem ============================================================================
setlocal
set "ACTION=%~1"
set "SCRIPT=%~dp0mpelb_update.py"
set "ROOT=%~dp0..\.."
if /i not "%ACTION%"=="--check" if /i not "%ACTION%"=="--update" (
  echo Usage: mpelb_update.cmd --check^|--update
  exit /b 1
)
if exist "%ROOT%\.venv\Scripts\python.exe" goto use_venv
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if not errorlevel 1 goto use_python
echo [mpelb-update] Python 3 not found; set up .venv per CONTRIBUTING.md
exit /b 1

:use_venv
"%ROOT%\.venv\Scripts\python.exe" "%SCRIPT%" %ACTION%
exit /b %ERRORLEVEL%

:use_py
py -3 "%SCRIPT%" %ACTION%
exit /b %ERRORLEVEL%

:use_python
python "%SCRIPT%" %ACTION%
exit /b %ERRORLEVEL%
