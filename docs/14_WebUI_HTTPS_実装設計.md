# Web UI + HTTPS（自己署名）実装設計

このドキュメントは `docs/13_WebブラウザUI.md` の内容を、実装に落とすための設計メモである。

## 目的

- CocoroGhost を **LAN 内の別端末** からブラウザで利用できるようにする
- 必須機能は次の通り
  - チャット（`/api/chat` の SSE ストリーム表示）
  - 画像付きチャット（`images: data:image/*;base64,...`）
  - 通知/リマインダー受信（`/api/events/stream` の WebSocket）
- LAN 内でも **HTTPS を必須**にする（ホスト認証は行わない）

## 非ゴール

- Web UI からの設定編集（`/api/settings` 相当）
- 会話履歴の永続表示（リロードで消えてよい）
- デスクトップウォッチ等、ネイティブ必須機能

## ポートと公開範囲

- 待受: `0.0.0.0:55601`
- Web UI と API は同一オリジン配信にする（CORS を発生させない）
- 公開範囲は OS 側（Windows ファイアウォール）で LAN 内に制限する（推奨）

## HTTPS（自己署名）設計

### 証明書の保存場所

- `config/tls/cert.pem`
- `config/tls/key.pem`

### 生成方針

- 起動時、上記ファイルが存在しなければ自動生成する
- 自己署名でよい（ブラウザ側は警告が出る前提）
- 将来の音声入力（`getUserMedia`）も見据え、HTTPS 前提に寄せる

### 生成手段（推奨）

- 外部コマンド（`openssl`）に依存せず、Python の `cryptography` で生成する
  - 配布（PyInstaller）時に「環境に openssl が無い」事故を避けるため
- 鍵種別は RSA 2048 を採用する（互換性優先）
- 証明書の SAN には最低限次を入れる
  - DNS: `localhost`
  - IP: `127.0.0.1`
  - IP: `::1`（可能なら）

注意:

- 端末が `https://<LAN IP>:55601/` でアクセスする場合、証明書の SAN にその IP が含まれないと「名前不一致」警告になる
  - 本設計では警告を許容する
  - 警告を完全に消すには端末側で証明書を信頼させる必要がある（本設計の必須要件にはしない）

## 認証設計（Bearer + Cookie セッション）

結論:

- ネイティブ連携は従来通り `Authorization: Bearer <TOKEN>` を継続
- Web UI は `POST /api/auth/login` でログインし、以後は Cookie セッションで認証する
- Web UI から使う主要 API（`POST /api/chat` / `WS /api/events/stream`）は **Bearer または Cookie のどちらでも通す**
  - ブラウザは Cookie を使う
  - 既存クライアントは Bearer を使う

理由:

- ブラウザ WebSocket は `Authorization` ヘッダを付与できないため

### Bearer の正（期待値）

- 現状の実装は `get_config_store().config.token` と照合する（TOML と settings.db の統合値）

### セッション Cookie（推奨）

- Cookie 属性:
  - `Secure`
  - `HttpOnly`
  - `SameSite=Strict`
- Cookie 名は `cocoro_session` とする（固定）
- セッション寿命:
  - 24 時間（アクセスがあれば延長）
- セッション保存先:
  - メモリ（プロセス再起動でログアウト）

実装メモ（最小）:

- `POST /api/auth/login` は JSON `{ "token": "..." }` を受け取る
- トークンが一致したら `session_id`（UUID）を発行し、`Set-Cookie` で返す
- サーバ側は `session_id -> expires_at` をメモリに保持し、アクセスごとに期限を延長する
- `POST /api/auth/logout` は Cookie のセッションを破棄し、Cookie を削除する（Max-Age=0）

### Web UI に公開しない API

- `GET /api/settings` は API キー等の秘密情報を含むため、Web UI から利用しない
  - 必要なら「Web UI 用の最小情報」だけ返す `GET /api/web/bootstrap` を追加する

## 端末を跨いだ会話継続（会話IDと端末IDを分ける）

要求:

- ネイティブ/ブラウザを含めて会話を共有する（端末を跨いで会話の続きになる）
- ただし `WS /api/events/stream` は「端末宛て配信（Vision等）」にも使うため、端末識別は壊さない

方針:

- ID を2種類に分ける
  - 会話継続用ID（`shared_conversation_id`）: サーバが 1つだけ持つ固定ID（永続化）
    - `/api/chat` は常に `shared_conversation_id` を使う（クライアントは `client_id` を送らない）
    - 永続化先は `settings.db`（単一ユーザー前提の単一行）
  - 端末識別用ID（`ws_client_id`）: 端末ごとに別ID（UIに見えなくてよい）
    - `WS /api/events/stream` の `hello.client_id` は `ws_client_id` として登録する
    - Web UI は `ws_client_id` を `localStorage` に保存して再利用する

同時送信:

- シンプルさ優先で、サーバ側で **プロセス内の同時実行を1本に制限**する（推奨）
  - 送信中に別端末から来たら、`POST /api/chat` は SSE の `event:error`（`code="chat_busy"`）を返して終了する

## Web UI（画面/挙動）

### 画面レイアウト（LINE風）

- 上部固定ヘッダ（ロゴ: `CocoroAI`）
- 中央: スクロール可能なチャット領域（左右のバブル）
- 下部固定: 入力欄（送信ボタン + 画像添付）

### ストリーミング表示（`/api/chat`）

- Web UI は `fetch` で `POST /api/chat` を呼び、レスポンスを SSE テキストとして逐次パースする
- 受信イベントの扱い（最小）:
  - `event: token` → 末尾に追記（アシスタント吹き出しを「生成中」で 1つ維持）
  - `event: done` → 確定（生成中状態を解除）
  - `event: error` → 短いエラー表示（生成中を終了）
- `/api/chat` は `embedding_preset_id` を要求しない（サーバのアクティブ設定を使用する）

### SSE パース実装（推奨）

- `fetch` の `response.body`（ReadableStream）を `TextDecoder` でデコードし、バッファへ追加する
- SSE イベントは `\n\n` 区切りで取り出す（チャンク境界で分割される前提）
- 1イベントの中で次を処理する
  - `event:` 行（省略時は `message` 扱いでよいが、本プロジェクトでは `token/done/error` を前提とする）
  - `data:` 行（複数行を許容し、`\n` で結合して JSON として扱う）
- 中断は `AbortController` を使い、UI側で「送信中」状態を確実に解除する

### 通知/リマインダー受信（`/api/events/stream`）

- Cookie セッション認証で WebSocket 接続する
- 受信 `type=notification|reminder` は、**AI 側の吹き出し**としてそのまま表示する
- WebSocket は切断し得るため、自動再接続（バックオフ）を入れる

### スクロール制御（最小）

- 通常は最下部へ追従する
- ユーザーが上にスクロールしたら追従を止め、「最新へ」ボタンを出す

## 静的ファイルの同梱（単一 exe 配布）

方針:

- FastAPI で静的配信する
  - `GET /` は `index.html` を返す
  - `GET /static/*` は静的ファイルを返す（FastAPI `StaticFiles`）
- 配布（PyInstaller onedir）に含めるため、`static/` をデータとしてバンドルする

補足:

- 現状の `/` は JSON を返しているため、Web UI 同梱時は `index.html` を返す構成に置き換える

## 追加するエンドポイント（設計）

- `GET /`（Web UI）
- `GET /static/*`（Web UI 静的ファイル）
- `POST /api/auth/login`（トークン入力 → セッション発行）
- `POST /api/auth/logout`（セッション破棄）

認証要件（要点）:

- Web UI から使う経路は「Cookie セッションで通る」こと
- ネイティブ連携は Bearer で今まで通り使えること
- `GET /api/settings` は Bearer のみに寄せ、Web UI では扱わないこと
