@echo off
rem ============================================================================
rem  NavKit v4 Studio - one-shot launcher: LocalBridge (mpelb) + Studio server.
rem  Both background processes start windowless (no console popup); the browser
rem  opens two tabs: MPE (auto-connects LocalBridge) and the Studio shell
rem  (ROI calibrator / policy table / template cropper on one port).
rem  Usage:
rem    studio.cmd                              defaults (root=repo, lb=26521, studio=26530)
rem    studio.cmd --root <dir> --port <n>      override root / LocalBridge port
rem    studio.cmd --studio-port <n>            override Studio server port
rem    studio.cmd --mpelb <exe>                explicit mpelb path
rem    studio.cmd --stop                       kill mpelb AND the studio server
rem  mpelb lookup: --mpelb > tools/navkit/dev/mpelb.exe > PATH > %APPDATA%
rem  NOTE: keep this file ASCII-only with CRLF line endings. cmd's batch
rem  parser byte-seeks and garbles the script when non-ASCII bytes mix with
rem  a mid-script chcp codepage switch.
rem ============================================================================
setlocal EnableDelayedExpansion

set "ROOT=%~dp0..\.."
set "LB_PORT=26521"
set "STUDIO_PORT=26530"
set "OVERRIDE="

:parse
if "%~1"=="" goto done_parse
if /i "%~1"=="--root"        ( set "ROOT=%~2"        & shift )
if /i "%~1"=="--port"        ( set "LB_PORT=%~2"     & shift )
if /i "%~1"=="--studio-port" ( set "STUDIO_PORT=%~2" & shift )
if /i "%~1"=="--mpelb"       ( set "OVERRIDE=%~2"    & shift )
if /i "%~1"=="--stop"        ( goto stop_only )
shift
goto parse
:done_parse

pushd "%ROOT%" >nul 2>&1 || ( echo [studio] bad root: "%ROOT%" & goto usage )
set "ROOT=%CD%"
popd

set "DEV_MPELB=%~dp0dev\mpelb.exe"
set "MPELB="
if not "%OVERRIDE%"=="" if exist "%OVERRIDE%" set "MPELB=%OVERRIDE%"
if "%MPELB%"=="" if exist "%DEV_MPELB%" set "MPELB=%DEV_MPELB%"
if "%MPELB%"=="" for /f "delims=" %%i in ('where mpelb 2^>nul') do if not defined MPELB set "MPELB=%%i"
if "%MPELB%"=="" if exist "%APPDATA%\MaaPipelineEditor\LocalBridge\mpelb.exe" set "MPELB=%APPDATA%\MaaPipelineEditor\LocalBridge\mpelb.exe"
if "%MPELB%"=="" (
  echo [studio] mpelb.exe not found. Put it in %~dp0dev\ or on PATH, or use --mpelb.
  goto usage
)

rem pythonw.exe is console-less; prefer it so the server never flashes a window
set "STUDIO_PY=%ROOT%\.venv\Scripts\pythonw.exe"
if not exist "%STUDIO_PY%" set "STUDIO_PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%STUDIO_PY%" (
  echo [studio] python not found under %ROOT%\.venv\Scripts\
  goto usage
)
set "STUDIO_SCRIPT=%ROOT%\tools\navkit\studio_server.py"

echo [studio] mpelb  : %MPELB%
echo [studio] root   : %ROOT%
echo [studio] ports  : lb=%LB_PORT% studio=%STUDIO_PORT%
echo [studio] python : %STUDIO_PY%

rem only one LB at a time for this tool
taskkill /f /im mpelb.exe >nul 2>&1

rem windowless start for both processes (Start-Process -WindowStyle Hidden)
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%MPELB%' -ArgumentList '--root','%ROOT%','--port','%LB_PORT%','--log-level','INFO' -WindowStyle Hidden" >nul 2>&1
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%STUDIO_PY%' -ArgumentList '%STUDIO_SCRIPT%','--port','%STUDIO_PORT%' -WindowStyle Hidden" >nul 2>&1

rem wait until BOTH ports are actually LISTENING (max ~20s), then open the tabs
set "TRIES=0"
:wait_ports
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr /r /c:":%LB_PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 goto wait_retry
netstat -ano | findstr /r /c:":%STUDIO_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 goto open_browser
:wait_retry
set /a TRIES+=1
if %TRIES% LSS 20 goto wait_ports
echo [studio] warning: ports not both listening after 20s, opening anyway.

:open_browser
start "" "https://mpe.codax.site/stable/?link_lb=true&port=%LB_PORT%"
start "" "http://127.0.0.1:%STUDIO_PORT%/"

echo.
echo [studio] Opened MPE (LocalBridge on port %LB_PORT%).
echo [studio] Opened Studio shell on http://127.0.0.1:%STUDIO_PORT%/
echo [studio] Use "studio.cmd --stop" to stop mpelb and the studio server.
exit /b 0

:stop_only
echo [studio] stopping mpelb...
taskkill /f /im mpelb.exe >nul 2>&1
echo [studio] stopping studio server on port %STUDIO_PORT%...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%STUDIO_PORT% .*LISTENING"') do taskkill /f /pid %%p >nul 2>&1
echo [studio] stopped.
exit /b 0

:usage
echo.
echo Usage: studio.cmd [--root ^<dir^>] [--port ^<n^>] [--studio-port ^<n^>] [--mpelb ^<exe^>] [--stop]
exit /b 1
