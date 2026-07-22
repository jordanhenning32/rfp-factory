@echo off
setlocal EnableExtensions

REM Launch only the curated, filesystem-isolated demo workspace. The normal
REM run_app.bat continues to use DATABASE_URL from the standard environment.

pushd "%~dp0.." >nul
if errorlevel 1 (
    echo ERROR: Could not enter the RFP Factory project directory.
    pause
    exit /b 1
)

set "DEMO_DATA_DIR=%CD%\data\demo"
set "DEMO_DATABASE=%DEMO_DATA_DIR%\sqlite.db"
set "DEMO_MANIFEST=%DEMO_DATA_DIR%\demo_manifest.json"
if not exist "%DEMO_DATABASE%" (
    echo.
    echo ERROR: Curated demo database was not found at:
    echo   %DEMO_DATABASE%
    echo Build or restore the demo dataset before launching.
    echo.
    popd
    pause
    exit /b 1
)
if not exist "%DEMO_MANIFEST%" (
    echo.
    echo ERROR: Demo completion marker was not found at:
    echo   %DEMO_MANIFEST%
    echo Rebuild the curated demo dataset before launching.
    echo.
    popd
    pause
    exit /b 1
)

set "RFP_DATA_DIR=%DEMO_DATA_DIR%"
set "DEMO_DATABASE_URL_PATH=%DEMO_DATABASE:\=/%"
set "DATABASE_URL=sqlite:///%DEMO_DATABASE_URL_PATH%"
set "APP_ENV=demo"

call "%~dp0run_app.bat" %*
set "DEMO_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %DEMO_EXIT_CODE%
