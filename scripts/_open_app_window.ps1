# Open the RFP Factory in Chrome (--app= mode for a clean standalone
# window without tabs or address bar) once the dev server is up.
#
# Designed to be backgrounded from scripts/run_app.bat with:
#   start /B "" powershell -NoProfile -ExecutionPolicy Bypass `
#       -File scripts/_open_app_window.ps1
#
# Falls back to the user's default browser if Chrome isn't installed.
# All errors are non-fatal — the dev server runs fine without the
# autopopen.

[CmdletBinding()]
param(
    [string]$Url = 'http://localhost:8000',
    [int]$DelaySeconds = 4
)

# Wait for the server to bind. NiceGUI takes ~3-4s on cold start.
Start-Sleep -Seconds $DelaySeconds

# Chrome can be in any of three standard locations depending on
# whether it was installed system-wide via the .msi (Program Files /
# Program Files (x86)) or user-local via the standard installer
# (LocalAppData). We probe in the order of "most-likely-fresh-install
# on a single-user dev machine" first.
$candidates = @(
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe')
)
$chrome = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

try {
    if ($chrome) {
        # --app= opens a dedicated window without browser chrome.
        # --new-window forces a separate window even if Chrome is
        # already running with other tabs — keeps the app isolated
        # so closing it doesn't drag in unrelated browsing state.
        Start-Process -FilePath $chrome -ArgumentList @(
            "--app=$Url",
            '--new-window'
        )
    } else {
        # Chrome not installed. Fall back to whatever the OS has
        # registered as the default for http:// (likely Edge on a
        # stock Windows install).
        Start-Process $Url
    }
} catch {
    # Don't propagate — the dev server is already running and can
    # be reached manually if the autopopen fails for some reason.
    Write-Warning "Auto-open failed: $($_.Exception.Message). Server is still running at $Url."
}
