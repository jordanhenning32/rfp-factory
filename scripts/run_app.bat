@echo off
setlocal EnableExtensions

REM Local/demo-safe RFP Factory launcher. Every gate below must pass before
REM the browser helper is started, so a bad environment never opens a dead UI.

title RFP Factory
pushd "%~dp0.." >nul
if errorlevel 1 (
    echo ERROR: Could not enter the RFP Factory project directory.
    pause
    exit /b 1
)

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo.
    echo ERROR: .venv Python was not found at "%PYTHON_EXE%"
    echo Create it once with: python -m venv .venv
    echo Then install dependencies with: .venv\Scripts\python.exe -m pip install -e .[llm_extra,dev]
    echo.
    popd
    pause
    exit /b 1
)

REM Empty process variables can override valid values in .env. Remove the
REM provider variables so pydantic-settings reads the local .env consistently.
set "ANTHROPIC_API_KEY="
set "OPENAI_API_KEY="
set "GOOGLE_API_KEY="
set "GEMINI_API_KEY="
set "GROK_API_KEY="

REM The desktop launcher is intentionally local-only, regardless of .env.
set "APP_HOST=127.0.0.1"
if not defined RFP_DEMO_PORT set "RFP_DEMO_PORT=8000"
set "APP_PORT=%RFP_DEMO_PORT%"
set "APP_URL=http://127.0.0.1:%APP_PORT%"

if /I "%~1"=="--preflight-only" goto :verify_only

echo.
echo Running startup preflight...
"%PYTHON_EXE%" scripts\launcher_preflight.py --phase before-migrations
if errorlevel 1 goto :preflight_failed

echo.
echo Applying database migrations...
"%PYTHON_EXE%" -m alembic upgrade head
if errorlevel 1 goto :migration_failed

echo.
echo Verifying migrated database...
"%PYTHON_EXE%" scripts\launcher_preflight.py --phase after-migrations
if errorlevel 1 goto :preflight_failed

echo.
echo ================================================================
echo  RFP Factory starting at %APP_URL%
echo  Browser will open after the health check succeeds.
echo  Close this window to stop the server.
echo ================================================================
echo.

REM This is deliberately below every failure gate. No browser is opened when
REM the environment, credentials, database, or migrations are not ready.
start /B "" powershell -NoProfile -ExecutionPolicy Bypass ^
    -File "%~dp0_open_app_window.ps1" -Url "%APP_URL%" -TimeoutSeconds 30

"%PYTHON_EXE%" -m app.main
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo ERROR: RFP Factory exited with code %APP_EXIT_CODE%.
    pause
)
popd
exit /b %APP_EXIT_CODE%

:verify_only
echo.
echo Running non-mutating readiness verification...
"%PYTHON_EXE%" scripts\launcher_preflight.py --phase verify
set "VERIFY_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %VERIFY_EXIT_CODE%

:preflight_failed
echo.
echo ERROR: Startup preflight failed. The server and browser were not started.
echo Fix the failures above, then run this launcher again.
popd
pause
exit /b 1

:migration_failed
echo.
echo ERROR: Database migration failed. The server and browser were not started.
echo Review the Alembic error above before trying again.
popd
pause
exit /b 1
