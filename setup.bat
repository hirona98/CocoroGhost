@echo off
echo ========================================
echo CocoroGhost セットアップスクリプト
echo ========================================
echo.

REM 仮想環境の確認
if not exist .venv (
    echo 仮想環境が存在しません。作成します...
    python -m venv .venv
    if errorlevel 1 (
        echo エラー: 仮想環境の作成に失敗しました
        pause
        exit /b 1
    )
    echo 仮想環境を作成しました
)

REM 仮想環境の活性化と依存パッケージのインストール
echo.
echo 依存パッケージをインストールします...
call .venv\Scripts\activate.bat
pip install -e .
if errorlevel 1 (
    echo エラー: パッケージのインストールに失敗しました
    pause
    exit /b 1
)

REM 設定ファイルの確認とコピー
echo.
if not exist config\setting.toml (
    echo 設定ファイルを作成します...
    copy config\setting.toml.example config\setting.toml
    if errorlevel 1 (
        echo エラー: 設定ファイルのコピーに失敗しました
        pause
        exit /b 1
    )
    echo 設定ファイルを作成しました: config\setting.toml
    echo 注意: config\setting.toml を編集してAPIキーなどを設定してください
) else (
    echo 設定ファイルは既に存在します
)

REM dataディレクトリの作成
if not exist data (
    echo dataディレクトリを作成します...
    mkdir data
)

echo.
echo ========================================
echo セットアップが完了しました！
echo ========================================
echo.
echo 起動方法:
echo   .venv\Scripts\activate
echo   python -X utf8 run.py
echo.
echo または:
echo   start.bat
echo.
pause
