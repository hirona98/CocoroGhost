@echo off
setlocal

REM ========================================
REM CocoroGhost Windows配布ビルド（onedir）
REM
REM - PyInstaller spec から dist\CocoroGhost\ を生成
REM - dist\CocoroGhost.exe（ルート直下の単体exe）は配布対象外なので削除
REM ========================================

REM --- プロジェクトルートへ移動（このbatの場所基準） ---
cd /d "%~dp0"

REM --- venv 有効化（無い場合はそのまま進む） ---
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
)

REM --- ユーザーsite-packages混入を防ぐ（PyInstallerのhookが拾って壊れるのを避ける） ---
set PYTHONNOUSERSITE=1

REM --- PyInstaller 実行（確認プロンプト無し） ---
if exist ".venv\Scripts\pyinstaller.exe" (
  ".venv\Scripts\pyinstaller.exe" --noconfirm cocoro_ghost_windows.spec
) else (
  REM venvが無い場合はPATH上のpyinstaller.exeを使う
  pyinstaller.exe --noconfirm cocoro_ghost_windows.spec
)
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller build failed.
  exit /b 1
)

REM --- onedir配布では dist\CocoroGhost\ を使うため、ルートの exe は削除 ---
if exist "dist\CocoroGhost.exe" (
  del /f /q "dist\CocoroGhost.exe"
)

echo.
echo [OK] Build finished.
echo [OK] Distribute: dist\CocoroGhost\

endlocal
