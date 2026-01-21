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
- `web_auto_login_enabled=true` の場合は Web UI 起動時に自動ログインする（トークン入力不要）
- 既存ネイティブクライアントは **Bearer トークン**で従来通り利用できる

セッション仕様（推奨）:

- Cookie: `Secure` / `HttpOnly` / `SameSite=Strict`
- Cookie 名: `cocoro_session`
- セッション寿命: 24 時間（スライディング更新）
- セッション保存先: メモリ（プロセス再起動でログアウト扱い）

## 端末を跨いだ会話継続（会話IDと端末IDを分ける）

目的:

- ネイティブ/ブラウザを含めて「同じ会話の続き」にする

方針:

- **会話継続用ID（`shared_conversation_id`）** と **端末識別用ID（`ws_client_id`）** を分ける
  - 会話継続用ID（`shared_conversation_id`）: サーバが 1つだけ持つ固定ID
    - `/api/chat` は常にこのIDで会話スレッドを扱う（クライアントは `client_id` を送らない）
    - サーバ再起動でも会話を継続するため、`settings.db` に永続化する
  - 端末識別用ID（`ws_client_id`）: 端末ごとに別ID（UIに見えなくてよい）
    - `WS /api/events/stream` 接続直後の `hello.client_id` は `ws_client_id` として登録する
    - 視覚（Vision）等の「宛先指定」配信は `ws_client_id` を前提とする（会話IDで上書きしない）

運用制約:

- 複数端末から同時にチャット送信すると会話が混ざり得る
- シンプルさ優先で、サーバ側で `shared_conversation_id` 単位の排他を入れる（推奨）
  - 送信中に別端末から来たら `409 Conflict` で弾く（UIは「別端末で送信中」程度の表示）

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
- `/api/chat` は `embedding_preset_id` を要求しない（サーバ側のアクティブ設定を使用する）

画像付きチャット:

- `<input type="file" multiple>` で画像を選択し、Data URI（`data:image/*;base64,...`）にして `images` に詰める
- クライアント側でも上限チェックのみ行う
  - 最大 5 枚
  - 1 枚 5MB 以下（デコード後）
  - 合計 20MB 以下（デコード後）

通知/リマインダー受信:

- `WS /api/events/stream` に接続し、`notification` / `reminder` を受信したら **AI 側の吹き出し**として表示する
- 接続直後に `hello` を送信してクライアント登録する
  - `hello.client_id` は `ws_client_id`（端末固有ID）
  - `ws_client_id` は Web UI なら `localStorage` に保存して再利用する（UIに表示しない）

## 追加/変更するエンドポイント（概要）

- `GET /`（Web UI）
- `GET /static/*`（Web UI 静的ファイル）
- `POST /api/auth/login`（トークン入力 → セッション Cookie 発行）
- `POST /api/auth/auto_login`（自動ログイン → セッション Cookie 発行、設定で有効なときのみ）
- `POST /api/auth/logout`（セッション破棄）

注記:

- `GET /api/settings` は秘密情報（API キー等）を含むため Web UI からは扱わない
- Web UI に追加で必要な最小情報が出てきた場合は `GET /api/web/bootstrap` のような専用 API を追加する（`/api/settings` は公開しない）
