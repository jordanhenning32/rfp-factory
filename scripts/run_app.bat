@echo off
REM Start the RFP Factory dev server.
REM
REM Replicates the manual PowerShell dev-server sequence:
REM   cd <project root>
REM   Remove-Item Env:ANTHROPIC_API_KEY,...,Env:GROK_API_KEY
REM   .venv\Scripts\activate
REM   python -m app.main
REM
REM The four API-key env-clears are load-bearing per CLAUDE.md:
REM   "PowerShell may have an empty env var overriding the file.
REM    Pydantic-settings prioritizes process env over .env."
REM We clear them so app.main reads the keys cleanly from .env.
REM
REM Console stays visible so the user can read live agent logs;
REM closing the window stops the server.

title RFP Factory
cd /d "%~dp0..\"

set "ANTHROPIC_API_KEY="
set "OPENAI_API_KEY="
set "GEMINI_API_KEY="
set "GROK_API_KEY="

if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo ERROR: .venv not found at %CD%\.venv
    echo Create it once with: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e .
    echo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo ================================================================
echo  RFP Factory starting at http://localhost:8000
echo  Browser will open automatically in ~4 seconds.
echo  Close this window to stop the server.
echo ================================================================
echo.

REM Background-launch the app window once the dev server has bound.
REM Delegated to scripts/_open_app_window.ps1 for legible probing of
REM the three standard Chrome install paths and a clean fallback to
REM the default browser. Chrome is launched in --app= mode so the
REM RFP Factory opens in a standalone window without tabs or address
REM bar — looks like a real desktop app, not a website in a tab.
REM
REM Errors are non-fatal — the dev server runs fine even if the
REM autopopen fails entirely (URL is printed above for manual nav).
start /B "" powershell -NoProfile -ExecutionPolicy Bypass ^
    -File "%~dp0_open_app_window.ps1"

python -m app.main
