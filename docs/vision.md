# 視覚（Vision）設計

本ドキュメントは、CocoroGhost（人格/記憶サーバ）に「視覚」を追加するための設計をまとめる。
短期は静止画（デスクトップ/外部カメラ）を対象とし、将来は動画にも拡張できる構造にする。

---

## 目的

- **同一チャットターン**で「見て→返答」まで完結する（ユーザーの発話に即応する）。
- **CocoroGhost が CocoroConsole に取得要求**を出す（将来、他クライアントへも拡張できる）。
- デスクトップの定期監視を **デスクトップウォッチ**として実装し、人格が能動的に確認してコメントする。

---

## 前提 / 今回のスコープ

- 対象は静止画のみ（`still`）。
- 対応ソースは `desktop` / `camera`。
- プライバシー（データ最小化・自動マスク等）は今回考慮しない。
  - ただし **有効/無効ボタン（desktop watch）**は実装する。
  - 有効化してから **5秒後に最初の1枚**を取得する。
- 失敗時の扱い
  - チャット起因の要求: 返答本文で人格として「見えない」等を返す。
  - デスクトップウォッチ: チャット（能動発話）でもログでもよい（実装で選べる）。
- 画像や要約は保存する（sensitivityは今回考慮しない）。
- カメラデバイス選択はクライアント（Console）側で行い、Ghostは指定しない。
- デスクトップ担当は **1台指名**する。

---

## 用語

- **client_id**
  - CocoroConsole（将来クライアント含む）の一意ID。
  - 「desktop担当1台指名」のために使用する。
- **request_id**
  - `capture_request` と `capture_response` を関連付ける相関ID（UUID推奨）。
- **デスクトップウォッチ**
  - AI人格が能動的にデスクトップを確認し、コメントする機能。
  - `/api/v2/notification` や `/api/v2/meta-request` と異なり、人格側が「見たい」気持ちで起動する。

---

## アーキテクチャ概要

### 方針

- Ghost→Console の要求は **WebSocket（/api/events/stream）** で送る。
  - GhostがConsoleのHTTPアドレスを知らなくてよい。
  - 複数クライアント接続にも拡張しやすい（client_idで宛先指定）。
- Console→Ghost の結果返却は **HTTP（/api/v2/vision/capture-response）** で返す。
  - 画像（Data URI）を安全に運ぶ（WSフレームサイズや再送制御をHTTP側で扱える）。

### 役割分担

- CocoroGhost
  - 「いつ」「何を」見るか（camera / desktop）を決める。
  - 画像を要約し、会話/能動コメントを生成し、Episodeとして保存する。
- CocoroConsole
  - 実際の画像取得（デスクトップキャプチャ/カメラ撮影）。
  - 取得した画像をGhostへ返す。
  - cameraデバイス選択・解像度等のユーザー設定を保持する。

---

## プロトコル（概要）

### WebSocket: `/api/events/stream`

既存の「Ghost→Consoleのイベント配信」に加えて、**Ghost→Consoleの命令**と **Console→Ghostの登録（hello）**を扱う。

#### Console → Ghost（接続直後 / 必須: Vision利用時）

```json
{
  "type": "hello",
  "client_id": "console-uuid-or-stable-id",
  "caps": ["vision.desktop", "vision.camera"]
}
```

- Ghostはこの情報をメモリ上に保持し、後続の宛先指定（Vision命令のターゲット指定）に用いる。
- `caps` は将来拡張（例: `video`）のための予約。

#### Ghost → Console（命令）

```json
{
  "type": "vision.capture_request",
  "data": {
    "request_id": "uuid",
    "source": "desktop|camera",
    "mode": "still",
    "purpose": "chat|desktop_watch",
    "timeout_ms": 5000
  }
}
```

- 宛先は「desktop担当」または「当該チャットのuser client」を client_id で指定する（配信側が選ぶ）。
- `timeout_ms` は5秒固定（今回）。

### HTTP: `POST /api/v2/vision/capture-response`

Consoleが `capture_request` の結果を返す。

```json
{
  "request_id": "uuid",
  "client_id": "console-id",
  "images": [
    "data:image/jpeg;base64,/9j/4AAQ..."
  ],
  "client_context": {
    "active_app": "string",
    "window_title": "string",
    "locale": "ja-JP"
  },
  "error": null
}
```

- 成功時は `images` に1枚以上を入れる（最大枚数は運用で制限する）。
- 失敗時は `error` に理由文字列を入れる（`images` は空）。

---

## チャット視覚（同一ターンで「見て→返答」）

### 要求

- ユーザー発話に「見てほしい」が含まれる場合は、カメラで取得して同一ターンで返答する。
- タイムアウトは5秒。
- 失敗時は「見えない」等を人格で返す。

### 実装方針（ストリーム先頭プレアンブル）

通常会話で毎回「判定専用LLM」を呼ぶとレイテンシが増える。
そのため、チャットのストリーミング出力の先頭に **内部プレアンブル（VISION_REQUESTの1行）** を出力させ、
サーバ側で先読み判定する方式を採用する。

1. LLMはストリームの最初の1行に `VISION_REQUEST: none|{...}` を出力する（ユーザーには見せない）。
2. `none` の場合は、そのまま同一LLM呼び出しの本文をストリーム配信する（追加LLM呼び出し無し）。
3. `{"source":"camera|desktop","extra_prompt":"..."}` の場合は、本文の配信を開始する前にキャプチャ要求を送り、最大5秒待つ。
4. キャプチャできたら、画像要約＋追加指示＋文脈で **本返答を再生成**して同一ターンで返す。
5. 失敗時は、人格として「見えない」等を返す（画像無しで再生成する）。

補足:
- `/api/chat` は `client_id` を必須とし、視覚要求の宛先に使用する。

---

## デスクトップウォッチ（能動視覚）

### 要求

- `desktop_watch_enabled` がONの間、指定間隔でデスクトップを確認する。
- 有効化から5秒後に最初の1枚を取得して確認する。
- desktop担当は1台指名する（`desktop_watch_target_client_id`）。
- 画像要約に対して人格がコメントし、Episodeとして保存する。

### 保存の考え方

- Episodeとして保存することで、後から「何をしていたか」「それに対して人格が何を言ったか」を辿れる。
- source は `desktop_watch` など専用値を用意し、チャット/通知/メタ要求と区別する。

### 実行フロー（概要）

1. 起動時にすでにONの場合は、`desktop_watch_interval_seconds` が経過してから初回の `vision.capture_request(source=desktop,purpose=desktop_watch)` を送る。
2. UI操作などで OFF→ON になったら 5秒後に初回の `vision.capture_request(source=desktop,purpose=desktop_watch)` を送る。
3. 以後、`desktop_watch_interval_seconds` ごとに同様の取得要求を送る。
4. `capture_response` 受信後、画像要約→能動コメント生成→Episode保存→イベント配信。
5. 失敗時はログ出力（必要なら控えめにイベントも出す）。

---

## 設定（settings.db / global_settings）

デスクトップウォッチのために、`global_settings` に以下を追加する想定。

- `desktop_watch_enabled`（INTEGER 0/1）
- `desktop_watch_interval_seconds`（INTEGER）
- `desktop_watch_target_client_id`（TEXT, nullable）

---

## 将来拡張（非スコープ）

- 動画（`mode=video`）: 別API（アップロード/ストリーミング）を用意する可能性が高い。
- 複数クライアント同時接続: desktop担当のフェイルオーバー、優先度、ヘルスチェック。
- プライバシー: マスク、アプリ/ウィンドウ名の扱い、保存抑制、感度区分（sensitivity）。
