@echo off
setlocal

REM ========================================
REM CocoroGhost Windows 配布ビルド（onedir）
REM
REM - PyInstaller spec から dist\CocoroGhost\ を生成
REM - dist\CocoroGhost.exe（dist直下の単体exe）は削除
REM ========================================

REM --- プロジェクトルートへ移動（このbatの場所基準） ---
cd /d "%~dp0"

REM --- venv 有効化（無い場合はそのまま進む） ---
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
)

REM --- ユーザーsite-packages混入を防ぐ（PyInstallerのhookが拾って壊れるのを避ける） ---
set PYTHONNOUSERSITE=1

REM --- dist が使用中で消せずにビルドが失敗するのを防ぐ ---
REM ※ CocoroGhost.exe を起動したままビルドすると dist\CocoroGhost がロックされることがあります
taskkill /f /im CocoroGhost.exe >nul 2>&1

set "DISTROOT=dist"
set "WORKROOT=build\cocoro_ghost_windows"

if not exist "%DISTROOT%" (
  mkdir "%DISTROOT%" || exit /b 1
)

REM --- PyInstaller 実行（確認プロンプト無し） ---
REM venv を必須にする（グローバル環境を使うと依存のmetadataが足りず失敗しやすい）
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [ERROR] .venv not found.
  echo [HINT] Run: setup.bat
  exit /b 1
)

REM PyInstaller が venv に入っていなければ導入する
".venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
  echo.
  echo [INFO] Installing pyinstaller into venv...
  ".venv\Scripts\python.exe" -m pip install pyinstaller
  if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install pyinstaller.
    exit /b 1
  )
)

REM venv の python.exe 経由で PyInstaller を実行する
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath "%DISTROOT%" --workpath "%WORKROOT%" cocoro_ghost_windows.spec
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller build failed.
  exit /b 1
)

REM --- onedir配布では %DISTROOT%\CocoroGhost\ を使うため、%DISTROOT% 直下の exe は削除 ---
if exist "%DISTROOT%\CocoroGhost.exe" (
  del /f /q "%DISTROOT%\CocoroGhost.exe"
)

set "OUTDIR=%DISTROOT%\CocoroGhost"

REM --- setting.toml.release を dist 側へコピー（exeの隣の config に置く） ---
if not exist "config\setting.toml.release" (
  echo.
  echo [ERROR] config\setting.toml.release not found.
  exit /b 1
)
if not exist "%OUTDIR%\config" (
  mkdir "%OUTDIR%\config" || exit /b 1
)
copy /y "config\setting.toml.release" "%OUTDIR%\config\setting.toml" >nul
if errorlevel 1 (
  echo.
  echo [ERROR] Failed to copy setting.toml.release to dist.
  exit /b 1
)

REM --- THIRD_PARTY_LICENSES.txt を dist 側へコピー（exeの隣に置く） ---
if not exist "docs\THIRD_PARTY_LICENSES.txt" (
  echo.
  echo [ERROR] docs\THIRD_PARTY_LICENSES.txt not found.
  echo [HINT] Run: .venv\Scripts\python.exe scripts\generate_third_party_licenses.py
  exit /b 1
)
copy /y "docs\THIRD_PARTY_LICENSES.txt" "%OUTDIR%\THIRD_PARTY_LICENSES.txt" >nul
if errorlevel 1 (
  echo.
  echo [ERROR] Failed to copy docs\THIRD_PARTY_LICENSES.txt to dist.
  exit /b 1
)


echo.
echo [OK] Build finished.
echo [OK] Distribute: %OUTDIR%\

endlocal
