# CocoroGhost

CocoroGhost は、CocoroAI Ver5 の会話・記憶処理を担う Python/FastAPI バックエンドです。  
単体で起動でき、`https://127.0.0.1:55601/`でアクセス可能です。
安全なローカル環境での実行を前提としています。

フルセットのCocoroAIはこちら
https://alice-encoder.booth.pm/items/6821221

Windows用のUIはこちら
https://github.com/hirona98/CocoroConsole

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

### 0. 前提（Ubuntu/Debian）

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv python3.12-venv
```

### 1. セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
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

<!-- Block: Linux Service -->
## ターミナルを閉じても実行する（Linux / systemd --user）

前提:

- `.venv` の作成と依存インストールが完了していること
- `APP_DIR` はこのリポジトリの絶対パスに置き換えること

### 1. ユーザーサービスを作成

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/cocoro-ghost.service <<'EOF'
[Unit]
Description=CocoroGhost
After=network.target

[Service]
Type=simple
WorkingDirectory=<APP_DIR>
Environment=COCORO_GHOST_HOME=<APP_DIR>
ExecStart=<APP_DIR>/.venv/bin/python -X utf8 -m cocoro_ghost.entrypoint
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
```

### 2. 起動と自動起動を有効化

```bash
systemctl --user daemon-reload
systemctl --user enable --now cocoro-ghost
```

### 3. ログアウト後も継続実行する設定

```bash
loginctl enable-linger "$USER"
```

### 4. 状態確認

```bash
systemctl --user status cocoro-ghost
journalctl --user -u cocoro-ghost -f
```

<!-- Block: Verify -->
## 動作確認

ブラウザ:

- `https://127.0.0.1:55601/`

ヘルスチェック:

```bash
curl -k https://127.0.0.1:55601/api/health
```

<!-- Block: Packaging -->
## Windows配布版作成（PyInstaller）

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
