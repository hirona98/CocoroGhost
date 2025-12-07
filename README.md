# cocoro_ghost

CocoroAIのLLMと記憶処理を担当するPython/FastAPIバックエンドサーバー

## 機能

- FastAPIによるREST APIサーバー
- LLMとの対話処理
- SQLiteベースのエピソード記憶管理
- sqlite-vecによるベクトル検索
- プリセット機能によるLLM設定の動的切り替え
- 画像クリーンアップなどの定期実行タスク

## セットアップ

### 自動セットアップ（推奨）

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
   - `env`: 環境（dev/prod）
   - （初回のみ）LLM/Embedding設定を入れておくと default プリセットが自動生成されます

   ※ DBファイルは `data/settings.db` と `data/memory_<memory_id>.db` に自動作成されます

## 起動方法

### バッチファイルで起動（推奨）

```bash
start.bat
```

### 手動起動

```bash
.venv\Scripts\activate
python -X utf8 run.py
```

サーバーは `http://0.0.0.0:55601` で起動します。

## 設定管理

#### 1. 基本設定（起動時必須）

`config/setting.toml` で以下を設定：

- `token`: API認証トークン
- `log_level`: ログレベル（DEBUG, INFO, WARNING, ERROR）
- `env`: 環境（dev/prod）
- （初回のみ）LLM/Embedding設定を入れておくと default プリセットが自動生成される

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
