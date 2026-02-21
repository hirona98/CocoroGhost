# CocoroGhost

CocoroGhost は、CocoroAI Ver5 の会話・記憶処理を担う Python/FastAPI バックエンドです。  
単体で起動でき、Web UI と API を同じプロセス・同じポートで提供します。

<!-- Block: Overview -->
## アプリ概要

- 会話 API（`/api/chat`）を SSE で配信し、トークンを逐次返す
- 記憶を SQLite（`sqlite-vec` 含む）で管理し、会話ごとに検索・更新する
- Web UI（`/`）を同梱し、ブラウザだけで利用できる
- HTTPS を常時有効化し、自己署名証明書を自動生成する

<!-- Block: Differences -->
## 特徴

1. **記憶品質を最優先に設計**  
速度より品質を重視します
会話ログを追記保存しつつ、「状態」を育てる構造で長期運用時の一貫性を重視しています。
1. **単一ユーザー運用を前提**  
シンプルな設計にするため単一ユーザーしか対応しません。
1. **SSE 開始前に必要記憶を同期確定**  
レスポンス開始後に検索を挟まないため、会話中の文脈ぶれを抑えます。
1. **記憶更新を非同期ジョブ化**  
WritePlan ベースで整理・索引化を分離し、会話体感と記憶品質を両立します。

<!-- Block: Docs -->
## ドキュメント

- 設計入口: `docs/00_はじめに.md`
- API 仕様: `docs/07_API.md`
- 実行フロー: `docs/10_実行フロー.md`
- Web UI: `docs/13_WebブラウザUI.md`
- Codex 作業索引: `docs/00_codex.md`

<!-- Block: Prerequisites -->
## 起動前の前提

- Python `3.10+`
- カレントディレクトリをリポジトリルートにする
- 初回起動前に `config/setting.toml` を作成する

`config/setting.toml` の最低限の編集項目:

- `token`: API 認証トークン
- `web_auto_login_enabled`: Web UI 自動ログイン可否
- `log_level`: ログレベル

注意:

- `config/setting.toml` は未知キーを許可しません（起動時にエラー）
- token の正は初回以降 `settings.db` 側です

<!-- Block: Windows Run -->
## 起動方法（Windows）

### 1. セットアップ

```bat
python.exe -m venv .venv
.\.venv\Scripts\activate
python.exe -m pip install --upgrade pip
python.exe -m pip install -e .
copy config\setting.toml.release config\setting.toml
```

### 2. 起動

配布向け挙動（`reload=False`）:

```bat
python.exe -X utf8 -m cocoro_ghost.entrypoint
```

開発向け挙動（`reload=True`）:

```bat
python.exe -X utf8 run.py
```

既存の補助スクリプトを使う場合:

```bat
start.bat
```

<!-- Block: Linux Run -->
## 起動方法（Linux）

### 1. セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .
cp config/setting.toml.release config/setting.toml
```

### 2. 起動

配布向け挙動（`reload=False`）:

```bash
python3 -X utf8 -m cocoro_ghost.entrypoint
```

開発向け挙動（`reload=True`）:

```bash
python3 -X utf8 run.py
```

<!-- Block: Verify -->
## 動作確認

ブラウザ:

- `https://127.0.0.1:55601/`

ヘルスチェック:

- Windows

```bat
curl.exe -k https://127.0.0.1:55601/api/health
```

- Linux

```bash
curl -k https://127.0.0.1:55601/api/health
```

<!-- Block: Paths -->
## 保存先（要点）

- 通常実行（非 frozen）: `config/` `logs/` `data/` は実行時 CWD 基準
- `COCORO_GHOST_HOME` を設定すると、そのパスを最優先で使用
- Windows 配布（PyInstaller frozen）では DB を `../UserData/Ghost/` に分離保存

<!-- Block: Packaging -->
## Windows配布（PyInstaller）

```bat
.\.venv\Scripts\activate
python.exe -m pip install pyinstaller
build.bat
```

spec から直接ビルドする場合:

```bat
pyinstaller.exe --noconfirm cocoro_ghost_windows.spec
```

生成物は `dist/CocoroGhost/` 配下です。
