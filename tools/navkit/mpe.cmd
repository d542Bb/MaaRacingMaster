@echo off
rem ============================================================================
rem  NavKit v4 MPE - C form entry (P3b): MPE IS the MPE, no shell.
rem  Start mpelb (root=repo root, indexes pipeline/ truth-source) then open MPE
rem  fullscreen in browser (auto-connects LocalBridge).
rem  Usage:
rem    mpe.cmd                          defaults (root=repo, port=26521)
rem    mpe.cmd --root <dir> --port <n>  override root / port
rem    mpe.cmd --mpelb <exe>            explicit mpelb path
rem    mpe.cmd --stop                   kill mpelb started by this tool
rem  mpelb lookup: --mpelb > tools/navkit/dev/mpelb.exe > PATH > %LOCALAPPDATA%
rem  NOTE: keep this file ASCII-only with CRLF line endings. cmd's batch
rem  parser byte-seeks and garbles the script when non-ASCII bytes mix with
rem  a mid-script chcp codepage switch.
rem ============================================================================
setlocal EnableDelayedExpansion

set "ROOT=%~dp0..\.."
set "PORT=26521"
set "OVERRIDE="

:parse
if "%~1"=="" goto done_parse
if /i "%~1"=="--root"  ( set "ROOT=%~2"         & shift )
if /i "%~1"=="--port"  ( set "PORT=%~2"         & shift )
if /i "%~1"=="--mpelb" ( set "OVERRIDE=%~2"     & shift )
if /i "%~1"=="--stop"  ( goto stop_only )
shift
goto parse
:done_parse

pushd "%ROOT%" >nul 2>&1 || ( echo [MPE] bad root: "%ROOT%" & goto usage )
set "ROOT=%CD%"
popd

set "DEV_MPELB=%~dp0dev\mpelb.exe"
set "MPELB="
if not "%OVERRIDE%"=="" if exist "%OVERRIDE%" set "MPELB=%OVERRIDE%"
if "%MPELB%"=="" if exist "%DEV_MPELB%" set "MPELB=%DEV_MPELB%"
if "%MPELB%"=="" for /f "delims=" %%i in ('where mpelb 2^>nul') do if not defined MPELB set "MPELB=%%i"
if "%MPELB%"=="" if exist "%LOCALAPPDATA%\MaaPipelineEditor\LocalBridge\mpelb.exe" set "MPELB=%LOCALAPPDATA%\MaaPipelineEditor\LocalBridge\mpelb.exe"
if "%MPELB%"=="" (
  echo [MPE] mpelb.exe not found. Put it in %~dp0dev\ or on PATH, or use --mpelb.
  goto usage
)

echo [MPE] mpelb : %MPELB%
echo [MPE] root  : %ROOT%
echo [MPE] port  : %PORT%

rem only one LB at a time for this tool
taskkill /f /im mpelb.exe >nul 2>&1

start "MaaRacingAssistant MPE LB" "%MPELB%" --root "%ROOT%" --port %PORT% --log-level INFO

rem ---- P3c: policy table page (standalone mini server, next to MPE) ----
set "POLICY_PORT=26530"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" if exist "%~dp0policy_server.py" (
  start "MaaRacingAssistant MPE policy" "%VENV_PY%" "%~dp0policy_server.py" --port %POLICY_PORT%
)

rem wait until mpelb WebSocket port is actually listening (max ~20s),
rem then open browser tabs so MPE connects on first load
set "TRIES=0"
:wait_lb
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 goto open_browser
set /a TRIES+=1
if %TRIES% LSS 20 goto wait_lb
echo [MPE] warning: mpelb port %PORT% not listening after 20s, opening anyway.

:open_browser
set "URL=https://mpe.codax.site/stable/?link_lb=true&port=%PORT%"
start "" "%URL%"
start "" "http://127.0.0.1:%POLICY_PORT%/"

echo.
echo [MPE] Opened MPE connecting LB on port %PORT%.
echo [MPE] Opened policy table on http://127.0.0.1:%POLICY_PORT%/ (rules editor).
echo [MPE] Use "mpe.cmd --stop" to stop the LocalBridge.
exit /b 0

:stop_only
echo [MPE] stopping mpelb...
taskkill /f /im mpelb.exe >nul 2>&1
echo [MPE] stopped.
exit /b 0

:usage
echo.
echo Usage: mpe.cmd [--root ^<dir^>] [--port ^<n^>] [--mpelb ^<exe^>] [--stop]
exit /b 1
