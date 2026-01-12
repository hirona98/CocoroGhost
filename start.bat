@echo off
setlocal

REM --- Run from project root (this .bat location) ---
cd /d "%~dp0"

REM --- Ensure UTF-8 behavior for Python output ---
set PYTHONUTF8=1

echo Starting CocoroGhost...

REM --- Ensure venv exists ---
if not exist ".venv\Scripts\python.exe" (
	echo [ERROR] .venv not found. Run setup.bat first.
	echo   cmd.exe /c setup.bat
	pause
	exit /b 1
)

REM --- Start server ---
".venv\Scripts\python.exe" -X utf8 run.py

endlocal

