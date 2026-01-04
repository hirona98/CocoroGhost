# CocoroGhost

CocoroAI Ver5（AI人格システム）のLLMと記憶処理を担当するPython/FastAPIバックエンドサーバー
CocoroConsoleやCocoroShell無しでの単独動作も可能とする

## 機能

- FastAPIによるREST APIサーバー
- LLMとの対話処理
- SQLiteベースのUnit記憶管理（Episode/Fact/Summary/Capsule/Loop）
- sqlite-vecによるベクトル検索
- プリセット機能によるLLM/Embedding/プロンプト設定の動的切り替え

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
   python -m venv .venv
   ```

2. **依存パッケージのインストール**
   ```bash
   .venv\Scripts\activate
   pip install -e .
   ```

   依存関係は `pyproject.toml` で管理されています。

3. **設定ファイルの準備**
   ```bash
   copy config\setting.toml.example config\setting.toml
   ```

4. **設定ファイルの編集**

   `config/setting.toml` を編集して、最小限の起動設定を記述：

   - `token`: API認証トークン
   - `log_level`: ログレベル

   ※ DBファイルは（実行場所の）`data/settings.db` と `data/memory_<embedding_preset_id>.db` に自動作成されます

## 起動方法

### バッチファイルで起動（推奨）

```bash
start.bat
```

## 設定管理

#### 1. 基本設定（起動時必須）

`config/setting.toml` で以下を設定：

- `token`: API認証トークン
- `log_level`: ログレベル（DEBUG, INFO, WARNING, ERROR）
- `log_file_enabled`: ファイルログの有効/無効
- `log_file_path`: ファイルログの保存先
- `log_file_max_bytes`: ログローテーションサイズ（bytes、既定は200000=200KB）

#### 2. LLM設定

設定DBにLLMなどの設定を保持


## 依存関係管理

依存関係は `pyproject.toml` で管理されています。以下のパッケージが含まれます：

- **fastapi** - Web フレームワーク
- **fastapi-utils** - FastAPI ユーティリティ
- **uvicorn[standard]** - ASGI サーバー
- **sqlalchemy** - ORM
- **litellm** - LLM クライアント
- **pydantic** - データバリデーション
- **python-multipart** - マルチパートフォームデータ処理
- **httpx** - HTTP クライアント
- **tomli** - TOML パーサー
- **sqlite-vec** - SQLite ベクトル検索拡張
- **typing_inspect** - 型チェックユーティリティ

新しい依存関係を追加する場合は `pyproject.toml` を編集して、再度 `pip install -e .` を実行してください。

## 開発時の注意

- Python実行時は必ず `-X utf8` オプションを付けること
- WSL環境ではpowershell.exe経由でPowerShellコマンドを実行すること
- 開発モードでインストール（`pip install -e .`）すると、コード変更が即座に反映されます

### 設定ファイルが見つからない

`config/setting.toml` が存在することを確認してください。存在しない場合：
```bash
copy config\setting.toml.example config\setting.toml
```

## Windows配布（PyInstaller）

配布方針:

- PyInstaller は `onedir` 前提（**設定/DB/ログを exe の隣に置く**ため）
- 実行時に `config/` `data/` `logs/` が exe と同じフォルダに作成/利用されます

### フォルダ構成（配布後）

- `CocoroGhost.exe`
- `config/setting.toml`（ユーザーが作成。テンプレ: `config/setting.toml.example`）
- `data/settings.db`（自動作成）
- `data/memory_<embedding_preset_id>.db`（自動作成）
- `data/reminders.db`（自動作成）
- `logs/`（ファイルログ有効時に作成）

### ビルド手順

1) 依存を入れる（開発環境）

```bash
.venv\Scripts\activate
pip install pyinstaller
```

2) ビルド（推奨: バッチ）

```bash
build_windows.bat
```

（補足）手動で spec からビルドする場合

```bash
pyinstaller.exe --noconfirm cocoro_ghost_windows.spec
```

3) 生成物

`dist/CocoroGhost/` 配下をそのまま配布してください。

NOTE:

- `dist/CocoroGhost.exe` も生成されることがありますが、`onedir` 配布では不要です（`build_windows.bat` は自動削除します）。

補足:

- 初回起動前に `config/setting.toml.example` を `config/setting.toml` にコピーし、`token` 等を編集してください。
