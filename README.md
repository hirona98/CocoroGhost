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
   - `db_url`: データベースURL
   - `log_level`: ログレベル
   - `env`: 環境（dev/prod）

   ※ LLMモデル設定はDBで管理されます（後述のプリセット機能参照）

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

### ハイブリッド設定方式

cocoro_ghostは2つの設定管理方式を採用しています：

#### 1. TOML設定（起動時必須）

`config/setting.toml` で以下を設定：

- `token`: API認証トークン
- `db_url`: データベースURL（デフォルト: sqlite:///./data/ghost.db）
- `log_level`: ログレベル（DEBUG, INFO, WARNING, ERROR）
- `env`: 環境（dev/prod）

#### 2. プリセット設定（動的変更可能）

LLMモデル設定や振る舞い設定はDBで管理され、API経由で動的に変更可能：

- `llm_api_key`: LLM APIキー
- `llm_model`: LLMモデル名
- `reflection_model`: リフレクション用モデル名
- `embedding_model`: 埋め込みモデル名
- `embedding_dimension`: 埋め込みベクトルの次元数
- `image_model`: 画像処理用モデル名
- `character_prompt`: キャラクター設定プロンプト
- `exclude_keywords`: 除外キーワードリスト
- その他の動的設定

### プリセット機能

複数の設定セット（プリセット）を名前付きで保存し、切り替えることができます：

- **初回起動**: TOMLにLLM設定がある場合、自動的に"default"プリセットを作成
- **プリセット作成**: `POST /presets` で新規プリセット作成
- **プリセット切り替え**: `POST /presets/{name}/activate` で切り替え（**再起動が必要**）
- **プリセット管理**: `GET /presets`、`PATCH /presets/{name}`、`DELETE /presets/{name}`

詳細は `docs/cocoro_ghost_api.md` を参照してください。

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

## トラブルシューティング

### sqlite-vec拡張が読み込めない

sqlite-vecは自動的にインストールされますが、問題がある場合は以下を確認：
- `pip install sqlite-vec` が正常に完了しているか
- Python 3.10以降を使用しているか

### 設定ファイルが見つからない

`config/setting.toml` が存在することを確認してください。存在しない場合：
```bash
copy config\setting.toml.example config\setting.toml
```
