@echo off
rem ==========================================================================
rem  Dev Shell launcher: run the repo Python sources inside the real GUI,
rem  no packaging step. Builds the .NET shell only when C#/frontend files are
rem  newer than the existing exe. See scripts\dev\run.ps1 header for why a
rem  packaged exe under build\ keeps running a stale source copy.
rem  Usage:
rem    dev-shell.cmd                      build-if-needed + launch (UAC prompt)
rem    dev-shell.cmd --rebuild            force dotnet build
rem    dev-shell.cmd --stop-existing      kill running MaaRM instances first
rem    dev-shell.cmd --check-deps         verify .venv imports before launch
rem    dev-shell.cmd --no-launch          build / verify only, no window
rem  NOTE: keep this file ASCII-only with CRLF line endings (batch parser).
rem ==========================================================================
setlocal EnableDelayedExpansion
set "ARGS="
:parse
if "%~1"=="" goto run
if /i "%~1"=="--rebuild"       ( set "ARGS=%ARGS% -Rebuild"      & shift & goto parse )
if /i "%~1"=="--stop-existing" ( set "ARGS=%ARGS% -StopExisting" & shift & goto parse )
if /i "%~1"=="--check-deps"    ( set "ARGS=%ARGS% -CheckDeps"    & shift & goto parse )
if /i "%~1"=="--no-launch"     ( set "ARGS=%ARGS% -NoLaunch"     & shift & goto parse )
if /i "%~1"=="--help"          goto usage
echo [dev-shell] unknown option: %~1
goto usage
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev\run.ps1"%ARGS%
exit /b %ERRORLEVEL%
:usage
echo Usage: dev-shell.cmd [--rebuild] [--stop-existing] [--check-deps] [--no-launch]
exit /b 0
