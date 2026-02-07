# CocoroGhost

CocoroAI Ver5（AI人格システム）のLLMと記憶処理を担当するPython/FastAPIバックエンドサーバー
CocoroConsoleやCocoroShell無しでの単独動作も可能とする

## 機能

- FastAPIによるREST APIサーバー
- LLMとの対話処理
- SQLite/sqlite-vecによる記憶管理

## 特徴

- AI人格システム専用の会話/記憶システム
- AI視点での記憶整理

## ドキュメント

- 仕様: `docs/README.md`

## セットアップ

### 自動セットアップ

```bash
setup.bat
```

このスクリプトは以下を自動で実行します：
- 仮想環境の作成（存在しない場合）
- 依存パッケージのインストール
- 設定ファイルの準備
- dataディレクトリの作成

### 手動セットアップ

1. **仮想環境の作成**
   ```bash
   python.exe -m venv .venv
   ```

2. **依存パッケージのインストール**
   ```bash
   .venv\Scripts\activate
   python.exe -m pip install -e .
   ```

   依存関係は `pyproject.toml` で管理されています。

3. **設定ファイルの準備**
   ```bash
   copy config\setting.toml.release config\setting.toml
   ```

4. **設定ファイルの編集**

   `config/setting.toml` を編集して、最小限の起動設定を記述：

   - `token`: API認証トークン（初回起動時に `settings.db` に保存され、以後は `settings.db` 側が正になる）
   - `web_auto_login_enabled`: Web UI 自動ログインの有効/無効
   - `log_level`: ログレベル

   ※ DBファイル（`settings.db` / `reminders.db` / `memory_<embedding_preset_id>.db`）は自動作成されます（保存先は「DB/パス」を参照）

## 起動方法

### バッチファイルで起動（推奨）

```bash
start.bat
```

## 設定管理

#### 1. 基本設定（起動時必須）

`config/setting.toml` で以下を設定：

- `token`: API認証トークン
- `web_auto_login_enabled`: Web UI 自動ログインの有効/無効
- `log_level`: ログレベル（DEBUG, INFO, WARNING, ERROR）
- `log_file_enabled`: ファイルログの有効/無効
- `log_file_path`: ファイルログの保存先
- `log_file_max_bytes`: ログローテーションサイズ（bytes、既定は200000=200KB）
- `llm_timeout_seconds`: LLMのタイムアウト秒数（非ストリーム、JSON生成など）
- `llm_stream_timeout_seconds`: LLMのタイムアウト秒数（ストリーム開始まで）

#### 2. LLM設定

設定DBにLLMなどの設定を保持


## 依存関係管理

依存関係は `pyproject.toml` で管理されています（安定動作のため、バージョンを固定しています）。以下のパッケージが含まれます：

- **fastapi** - Web フレームワーク
- **uvicorn[standard]** - ASGI サーバー
- **sqlalchemy** - ORM
- **litellm==1.81.6** - LLM クライアント
- **pydantic** - データバリデーション
- **python-multipart** - マルチパートフォームデータ処理
- **httpx** - HTTP クライアント
- **tomli** - TOML パーサー
- **sqlite-vec** - SQLite ベクトル検索拡張
- **typing_inspect** - 型チェックユーティリティ

新しい依存関係を追加する場合は `pyproject.toml` を編集して、再度 `pip install -e .` を実行してください。

## 開発時の注意

- Python実行時は必ず `-X utf8` オプションを付けること
- 開発モードでインストール（`pip install -e .`）すると、コード変更が即座に反映されます

### 設定ファイルが見つからない

`config/setting.toml` が存在することを確認してください。存在しない場合：
```bash
copy config\setting.toml.release config\setting.toml
```

## DB/パス

保存先の方針は `cocoro_ghost/paths.py` に準拠する。

- 通常実行（非frozen）:
  - `config/` / `logs/` / `data/` は `app_root` 直下
  - `app_root` は基本 CWD（例: `start.bat` の実行場所）
  - `COCORO_GHOST_HOME` があれば最優先で `app_root` になる
- Windows配布（PyInstaller frozen）:
  - `config/` / `logs/` は exe の隣（`<exe_dir>/config` / `<exe_dir>/logs`）
  - DBは exe の 1つ上の `UserData/Ghost/` に保存する（`<exe_dir>/../UserData/Ghost`）

## Windows配布（PyInstaller）

配布方針:

- PyInstaller は `onedir` 前提（配布をシンプルにする）
- 設定とログは exe の隣に置く（`<exe_dir>/config` / `<exe_dir>/logs`）
- DBはユーザーデータとして exe と分離し、`<exe_dir>/../UserData/Ghost` に保存する

### フォルダ構成（配布後）

- `CocoroGhost.exe`
- `config/setting.toml`（ユーザーが作成。テンプレ: `config/setting.toml.release`）
- `logs/`（ファイルログ有効時に作成）
- `../UserData/Ghost/settings.db`（自動作成）
- `../UserData/Ghost/memory_<embedding_preset_id>.db`（自動作成）
- `../UserData/Ghost/reminders.db`（自動作成）

### ビルド手順

1) 依存を入れる（開発環境）

```bash
.venv\Scripts\activate
python.exe -m pip install pyinstaller
```

2) ビルド（推奨: バッチ）

```bash
build.bat
```

（補足）手動で spec からビルドする場合

```bash
pyinstaller.exe --noconfirm cocoro_ghost_windows.spec
```

3) 生成物

`dist/CocoroGhost/` 配下をそのまま配布してください。


補足:

- 初回起動前に `config/setting.toml.release` を `config/setting.toml` にコピーし、`token` 等を編集してください。
