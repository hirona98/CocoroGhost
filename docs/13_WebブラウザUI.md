# WebブラウザUI（LAN / HTTPS / セッション）

このドキュメントは、Web UI の概要（利用者向け）と実装メモ（開発者向け）を統合したもの。
以前分割されていた実装設計は、本ファイルへ統合済み。

目的:

- CocoroGhost を **LAN 内の別端末（スマホ/別PC）** からブラウザで使えるようにする
- 最初に必要なのは **チャット / 画像付きチャット / 通知 / リマインダー受信 / 設定編集**

非ゴール:

- デスクトップウォッチ等「ネイティブ必須」の機能
- 会話履歴の永続表示（リロードで消えてよい）

## 全体構成（1プロセス / 1ポート）

- CocoroGhost（FastAPI + uvicorn）が、同一ポートで以下を提供する
  - Web UI（静的ファイル）
  - 既存 API（`/api/*`）
- 待ち受けは `0.0.0.0:<cocoro_ghost_port>`（LAN 公開、既定: 55601）
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
- 将来の音声入力（`getUserMedia`）も見据え、HTTPS 前提に寄せる

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

実装メモ（最小）:

- `POST /api/auth/login` は JSON `{ "token": "..." }` を受け取り、成功時に Cookie セッションを発行する
- `POST /api/auth/logout` はセッションを破棄し、Cookie を削除する

注記（Web UI と `/api/settings`）:

- Web UI は Cookie セッション認証で `GET /api/settings` と `PUT /api/settings` を利用できる
- 単一ユーザー運用前提のため、Web UI 上で API キー等を含む設定フォームを直接編集する

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
- シンプルさ優先で、サーバ側で **プロセス内の同時実行を1本に制限**する（推奨）
  - 送信中に別端末から来たら、`POST /api/chat` は SSE の `event:error`（`code="chat_busy"`）を返して終了する
  - UIは「他のチャット処理中」程度の短い表示でよい

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

SSE パース実装（推奨）:

- `fetch` の `response.body`（ReadableStream）を `TextDecoder` でデコードし、バッファへ追加する
- SSE イベントは `\n\n` 区切りで取り出す（チャンク境界で分割される前提）
- 1イベントの中で次を処理する
  - `event:` 行
  - `data:` 行（複数行を許容し、`\n` で結合して JSON として扱う）
- 中断は `AbortController` を使い、UI側で「送信中」状態を確実に解除する

画像付きチャット:

- `<input type="file" multiple>` で画像を選択し、Data URI（`data:image/*;base64,...`）にして `images` に詰める
- クライアント側でも上限チェックのみ行う
  - 最大 5 枚
  - 1 枚 5MB 以下（デコード後）
  - 合計 20MB 以下（デコード後）

設定編集:

- 右上の設定アイコン（⚙）で設定画面を開く
- 設定画面はタブ（ペルソナ/会話/記憶/システム）で項目を分割する
- ペルソナタブ内で、ペルソナプリセットとアドオンプリセットを選択し、追加/複製/削除を行えるようにする
- 記憶タブでは `memory_enabled` を常時ONとして扱い、OFF切替UIは出さない
- 記憶タブは `Embeddingプリセット` のグループのみを表示する（記憶機能の独立グループは置かない）
- 記憶タブには赤字の注意文（「記憶機能はOFF不可」「赤字項目は一度設定したら変更しない」）を表示する
- 記憶タブでは `Embeddingモデル` と `Embedding次元数` のラベルを赤字表示する
- 編集値はフォームへ反映し、保存時に `PUT /api/settings` の全量更新で確定する
- 保存成功時は、サーバから返された最新設定をフォームへ再描画する（サーバ正を表示）
- 設定画面上部は2段構成にする（1段目: 左に `チャットに戻る` / 右に `ログアウト`、2段目: 左に `再読込` と `保存`）
- 設定画面内からログアウト可能にする

通知/リマインダー受信:

- `WS /api/events/stream` に接続し、`notification` / `reminder` を受信したら **AI 側の吹き出し**として表示する
- 接続直後に `hello` を送信してクライアント登録する
  - `hello.client_id` は `ws_client_id`（端末固有ID）
  - `ws_client_id` は Web UI なら `localStorage` に保存して再利用する（UIに表示しない）

WebSocket 再接続（推奨）:

- WebSocket は切断し得るため、自動再接続（バックオフ）を入れる

スクロール制御（最小）:

- 通常は最下部へ追従する
- ユーザーが上にスクロールしたら追従を止め、「最新へ」ボタンを出す

## 静的ファイルの同梱（単一 exe 配布）

方針:

- FastAPI で静的配信する
  - `GET /` は `index.html` を返す
  - `GET /static/*` は静的ファイルを返す（FastAPI `StaticFiles`）
- 配布（PyInstaller onedir）に含めるため、`static/` をデータとしてバンドルする
