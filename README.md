# cocoro_ghost

CocoroAIのLLMと記憶処理を担当するPython/FastAPIバックエンドサーバー

## 機能

- FastAPIによるREST APIサーバー
- LLMとの対話処理
- SQLiteベースのエピソード記憶管理
- sqlite-vecによるベクトル検索
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
   copy config\ghost.toml.example config\ghost.toml
   ```

4. **設定ファイルの編集**

   `config/ghost.toml` を編集して、以下を設定：
   - `token`: API認証トークン
   - `llm_model`: 使用するLLMモデル
   - その他の設定項目

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

## 設定ファイル

`config/ghost.toml` で以下を設定できます：

- `token`: API認証トークン
- `db_url`: データベースURL（デフォルト: sqlite:///./data/ghost.db）
- `llm_model`: LLMモデル名
- `reflection_model`: リフレクション用モデル名
- `embedding_model`: 埋め込みモデル名
- `embedding_dimension`: 埋め込みベクトルの次元数
- `image_model`: 画像処理用モデル名
- `log_level`: ログレベル（DEBUG, INFO, WARNING, ERROR）
- その他の設定項目

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

`config/ghost.toml` が存在することを確認してください。存在しない場合：
```bash
copy config\ghost.toml.example config\ghost.toml
```
