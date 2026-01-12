@echo off
setlocal

REM ========================================
REM CocoroGhost setup (Windows)
REM - Creates venv if missing
REM - Installs dependencies into venv
REM - Prepares config + data directory
REM ========================================

REM --- Always run from project root (this .bat location) ---
cd /d "%~dp0"

REM --- Ensure UTF-8 behavior for Python output ---
set PYTHONUTF8=1

echo ========================================
echo CocoroGhost setup
echo ========================================
echo.

REM --- Create venv if missing ---
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating venv .venv...

    REM Prefer Python Launcher if available
    where.exe py.exe >nul 2>&1
    if %errorlevel%==0 (
        py.exe -3.10 -m venv .venv
        if errorlevel 1 (
            py.exe -3 -m venv .venv
        )
    ) else (
        REM Fallback: python.exe must be on PATH
        where.exe python.exe >nul 2>&1
        if not %errorlevel%==0 (
            echo [ERROR] Python not found. Install Python 3.10+ and ensure python.exe is on PATH.
            pause
            exit /b 1
        )
        python.exe -m venv .venv
    )

    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
)

REM --- Install dependencies into venv (no activate needed) ---
echo.
echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
    echo [ERROR] Failed to install package: pip install -e .
    pause
    exit /b 1
)

REM --- Prepare config ---
echo.
echo [3/3] Preparing config/data...
if not exist "config\setting.toml" (
    if not exist "config\setting.toml.release" (
        echo [ERROR] config\setting.toml.release not found.
        pause
        exit /b 1
    )
    copy /y "config\setting.toml.release" "config\setting.toml" >nul
    if errorlevel 1 (
        echo [ERROR] Failed to create config\setting.toml
        pause
        exit /b 1
    )
    echo Created: config\setting.toml
) else (
    echo Config exists: config\setting.toml
)

REM --- Ensure data directory exists ---
if not exist "data" (
    mkdir "data"
)

echo.
echo ========================================
echo Setup completed.
echo ========================================
echo.
echo Next:
echo   - Edit config\setting.toml
echo   - Run: start.bat
echo.
pause

endlocal

