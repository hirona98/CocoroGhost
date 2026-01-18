# WebブラウザUI（LAN / HTTPS / セッション）

目的:

- CocoroGhost を **LAN 内の別端末（スマホ/別PC）** からブラウザで使えるようにする
- 最初に必要なのは **チャット / 画像付きチャット / 通知 / リマインダー受信**

非ゴール:

- デスクトップウォッチ等「ネイティブ必須」の機能
- Web UI からの設定編集（`/api/settings` 相当）
- 会話履歴の永続表示（リロードで消えてよい）

## 全体構成（1プロセス / 1ポート）

- CocoroGhost（FastAPI + uvicorn）が、同一ポートで以下を提供する
  - Web UI（静的ファイル）
  - 既存 API（`/api/*`）
- 待ち受けは `0.0.0.0:55601`（LAN 公開）
- Web UI は同一オリジンで API を呼ぶ（CORS 不要）

## HTTPS（自己署名）

- LAN でも HTTPS を必須にする
- 証明書は自己署名でよい（ホスト認証は行わない）
  - その代わり、ブラウザ側では証明書警告が出る前提になる
- 起動時に `config/tls/` の証明書が無ければ自動生成する
  - `config/tls/cert.pem`
  - `config/tls/key.pem`

運用メモ:

- 「警告を完全に消す」には端末側で証明書を信頼させる必要がある（本設計では必須にしない）
- 公開範囲は Windows ファイアウォールで LAN 内に制限すること（推奨）

## 認証（ブラウザは Cookie セッション）

前提:

- ブラウザ WebSocket は `Authorization` ヘッダを付与できない

方針:

- Web UI はログインして **Cookie セッション**で認証する
- 既存ネイティブクライアントは **Bearer トークン**で従来通り利用できる

セッション仕様（推奨）:

- Cookie: `Secure` / `HttpOnly` / `SameSite=Strict`
- セッション寿命: 24 時間（スライディング更新）
- セッション保存先: メモリ（プロセス再起動でログアウト扱い）

## shared client_id（全クライアントで会話を共有）

目的:

- ネイティブ/ブラウザを含めて「同じ会話の続き」にする

方針:

- サーバに `shared_client_id` を 1つ持たせる
- クライアントが送ってきた `client_id` は採用せず、常に `shared_client_id` を使用する

運用制約:

- 複数端末から同時に送信すると会話が混ざり得る
- 初期は「同時送信しない」運用でよい（サーバ側の厳密な排他は必須にしない）

## Web UI 仕様（最小）

画面:

- 上部固定ヘッダ（ロゴ: `CocoroAI`）
- 中央: スクロール領域（バブル式チャット）
- 下部固定: 入力欄（送信ボタン + 画像添付）

チャット送信:

- `POST /api/chat` を `fetch` で呼び出し、レスポンス本文を SSE として逐次パースして描画する
  - `event: token` のたびに「生成中」吹き出しへ追記
  - `event: done` で確定
  - `event: error` は吹き出し内で短く表示

画像付きチャット:

- `<input type="file" multiple>` で画像を選択し、Data URI（`data:image/*;base64,...`）にして `images` に詰める
- クライアント側でも上限チェックのみ行う
  - 最大 5 枚
  - 1 枚 5MB 以下（デコード後）
  - 合計 20MB 以下（デコード後）

通知/リマインダー受信:

- `WS /api/events/stream` に接続し、`notification` / `reminder` を受信したら **AI 側の吹き出し**として表示する
- 接続直後に `hello` を送信してクライアント登録する

## 追加/変更するエンドポイント（概要）

- `GET /`（Web UI）
- `POST /api/auth/login`（トークン入力 → セッション Cookie 発行）
- `POST /api/auth/logout`（セッション破棄）

注記:

- `GET /api/settings` は秘密情報（API キー等）を含むため Web UI からは扱わない
- Web UI に必要な最小情報がある場合は `GET /api/web/bootstrap` のような専用 API を追加する
