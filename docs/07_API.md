# API

## ベースパス

- APIは `/api` をプレフィックスにする（例: `/api/chat`）

## 認証

- `Authorization: Bearer <TOKEN>`
- トークンの正は `settings.db` に置く

注記:

- `/api/settings` ではトークンを更新しない（変更する場合は `settings.db` を編集して再起動）

## `/api/health`

ヘルスチェック。

### `GET /api/health`

```json
{ "status": "healthy" }
```

## `/api/chat`（SSE）

### リクエスト（例）

```json
{
  "embedding_preset_id": "uuid",
  "client_id": "stable-client-id",
  "input_text": "string",
  "images": [{"type":"image","base64":"..."}],
  "client_context": {"active_app":"...", "window_title":"...", "locale":"ja-JP"}
}
```

### SSEイベント（例）

```text
event: token
data: {"text":"..."}

event: done
data: {"event_id":123,"reply_text":"...","usage":{}}

event: error
data: {"message":"...","code":"..."}
```

補足:

- `event_id` は「このターンの出来事ログ（`events`）」を指すID（整数、`INTEGER`）とする

## `/api/v2/notification`

通知（外部システム→Ghost）。

### リクエスト（JSON）

```json
{
  "source_system": "アプリ名",
  "text": "通知メッセージ",
  "images": [
    "data:image/jpeg;base64,/9j/4AAQ...",
    "data:image/png;base64,iVBORw0KGgo..."
  ]
}
```

- `images` は省略可能（最大5枚）
- `images` の要素は `data:image/*;base64,...` 形式の Data URI

### レスポンス

- `204 No Content`

### 例（`curl.exe`）

```bash
curl.exe -X POST http://127.0.0.1:55601/api/v2/notification \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d "{\"source_system\":\"MyApp\",\"text\":\"処理完了\",\"images\":[\"data:image/jpeg;base64,...\"]}"
```

### 例（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:55601/api/v2/notification" `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{ Authorization = "Bearer <TOKEN>" } `
  -Body '{"source_system":"MyApp","text":"結果","images":["data:image/jpeg;base64,...","data:image/png;base64,..."]}'
```

補足:

- HTTPレスポンスは先に返り、AI人格のセリフ（`data.message`）は `/api/events/stream` で後から届く

## `/api/v2/meta-request`

メタ依頼（外部システム→Ghost）。

### リクエスト（JSON）

```json
{
  "instruction": "任意の指示",
  "payload_text": "任意の本文（省略可）",
  "images": [
    "data:image/jpeg;base64,/9j/4AAQ...",
    "data:image/png;base64,iVBORw0KGgo..."
  ]
}
```

- `images` は省略可能（最大5枚）
- `images` の要素は `data:image/*;base64,...` 形式の Data URI

### レスポンス

- `204 No Content`

### 例（`curl.exe`）

```bash
curl.exe -X POST http://127.0.0.1:55601/api/v2/meta-request \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d "{\"instruction\":\"これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。\",\"payload_text\":\"～ニュース内容～\"}"
```

### 例（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:55601/api/v2/meta-request" `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{ Authorization = "Bearer <TOKEN>" } `
  -Body '{"instruction":"これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。","payload_text":"～ニュース内容～"}'
```

補足:

- HTTPレスポンスは先に返り、AI人格のセリフ（`data.message`）は `/api/events/stream` で後から届く
- `instruction` / `payload_text` は永続化しない（生成にのみ利用）
- ただし、**それをきっかけに AI 人格が「自分で思いついて動いた」結果（出来事ログ）は永続化する**（外部要求の存在を想起させる情報は残さない）
  - その出来事ログは `events.source="meta_proactive"` として区別する

## `/api/v2/vision/capture-response`

クライアントが `/api/events/stream` で受け取った `vision.capture_request` に対して、画像取得結果を返す。

### リクエスト（JSON）

```json
{
  "request_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "client_id": "console-uuid-or-stable-id",
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

- `request_id` は `vision.capture_request.data.request_id` と一致させる（相関用）
- `images` は Data URI 形式
- `error` が `null` でない場合、`images` は空でよい（スキップ/失敗の区別は `error` の値で行う）

### `error` の仕様

- 成功: `error: null` かつ `images` が1枚以上
- スキップ（正常系）: `images: []` かつ `error` が以下のいずれか
  - `capture skipped (idle)`
  - `capture skipped (excluded window title)`
- 失敗（異常系）: `images: []` かつ `error` が上記以外の任意文字列

### レスポンス

- `204 No Content`

補足:

- `images` は保持しない（DBに保存しない）
- 画像は LLM で **詳細な説明テキスト**に変換し、それを出来事ログ（`events`）に残す

## `/api/settings`

UI向けの「全設定」取得/更新。

### `GET /api/settings`

#### レスポンス（例）

```json
{
  "memory_enabled": true,
  "desktop_watch_enabled": false,
  "desktop_watch_interval_seconds": 300,
  "desktop_watch_target_client_id": "console-uuid-or-stable-id",
  "active_llm_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "active_embedding_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "active_persona_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "active_addon_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "llm_preset": [
    {
      "llm_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "llm_preset_name": "default",
      "llm_api_key": "string",
      "llm_model": "string",
      "reasoning_effort": "optional",
      "llm_base_url": "optional",
      "max_turns_window": 20,
      "max_tokens": 2048,
      "image_model_api_key": "optional",
      "image_model": "string",
      "image_llm_base_url": "optional",
      "max_tokens_vision": 1024,
      "image_timeout_seconds": 30
    }
  ],
  "embedding_preset": [
    {
      "embedding_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "embedding_preset_name": "default",
      "embedding_model_api_key": "optional",
      "embedding_model": "string",
      "embedding_base_url": "optional",
      "embedding_dimension": 1536,
      "similar_episodes_limit": 60
    }
  ],
  "persona_preset": [
    {
      "persona_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "persona_preset_name": "default",
      "persona_text": "string"
    }
  ],
  "addon_preset": [
    {
      "addon_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "addon_preset_name": "default",
      "addon_text": "string"
    }
  ]
}
```

### `PUT /api/settings`

全設定をまとめて確定（共通設定 + プリセット一覧 + `active_*_preset_id`）。

注記:

- `*_preset` 各配列は「最終的に残したいプリセットの完成形」を送る（全置換コミット）
- サーバ側は `*_preset_id`（UUID）で `upsert` する
- リクエストに含まれない既存プリセットは削除せず `archived=true` にする
- `GET /api/settings` は `archived=false` のもののみ返す

#### リクエスト（例）

`GET /api/settings` と同じ形式。

#### レスポンス

- `GET /api/settings` と同じ（更新後の状態を返す）

## `/api/reminders/*`

リマインダーは `/api/settings` ではなく、専用APIで管理する（全置換を避けるため）。

- `GET /api/reminders/settings`
- `PUT /api/reminders/settings`
- `GET /api/reminders`
- `POST /api/reminders`
- `PATCH /api/reminders/{id}`
- `DELETE /api/reminders/{id}`

### `GET /api/reminders/settings`

```json
{
  "reminders_enabled": true,
  "target_client_id": "console-uuid-or-stable-id"
}
```

### `PUT /api/reminders/settings`

```json
{
  "reminders_enabled": true,
  "target_client_id": "console-uuid-or-stable-id"
}
```

### `GET /api/reminders`

```json
{
  "items": [
    {
      "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "enabled": true,
      "repeat_kind": "once|daily|weekly",
      "content": "string",
      "scheduled_at": "2026-01-03T09:30:00+09:00",
      "time_of_day": "09:30",
      "weekdays": ["sun","mon"],
      "next_fire_at_utc": 1767400200
    }
  ]
}
```

### `POST /api/reminders`（例）

- `repeat_kind=once`

```json
{
  "enabled": true,
  "repeat_kind": "once",
  "scheduled_at": "2026-01-03T09:30:00+09:00",
  "content": "string"
}
```

- `repeat_kind=daily`

```json
{
  "enabled": true,
  "repeat_kind": "daily",
  "time_of_day": "09:30",
  "content": "string"
}
```

- `repeat_kind=weekly`

```json
{
  "enabled": true,
  "repeat_kind": "weekly",
  "time_of_day": "09:30",
  "weekdays": ["sun","wed","fri"],
  "content": "string"
}
```

## `/api/logs/stream`（WebSocket）

サーバログの購読（テキストフレームでJSONを送信）。

- URL: `ws(s)://<host>/api/logs/stream`
- 認証: `Authorization: Bearer <TOKEN>`

接続直後に直近最大500件のバッファを送信し、その後も新規ログを随時送信する。

```json
{"ts":"2025-12-13T10:00:00+00:00","level":"INFO","logger":"cocoro_ghost.main","msg":"string"}
```

## `/api/events/stream`（WebSocket）

- URL: `ws(s)://<host>/api/events/stream`
- 認証: `Authorization: Bearer <TOKEN>`
- 目的:
  - `POST /api/v2/notification` / `POST /api/v2/meta-request` を受信したとき、接続中クライアントへイベントを配信する
  - リマインダーが発火したとき、`target_client_id` 宛てに `reminder` を配信する
  - 視覚のための `vision.capture_request` をクライアントへ送る（命令）

接続直後に最大200件のバッファ済みイベントを送信し、その後は新規イベントをリアルタイムで送信する。

### サーバ→クライアント（JSON）

```json
{
  "event_id": 123,
  "type": "notification|meta-request|desktop_watch|reminder|vision.capture_request",
  "data": {
    "system_text": "string",
    "message": "string"
  }
}
```

補足:

- 保存しない命令（例: `vision.capture_request`）は `event_id: 0` とする

例（`type="notification"`）:

```json
{
  "event_id": 123,
  "type": "notification",
  "data": {
    "system_text": "[notificationのfrom] notificationのmessage",
    "message": "AI人格のセリフ"
  }
}
```

例（`type="meta-request"`）:

```json
{
  "event_id": 123,
  "type": "meta-request",
  "data": {
    "message": "AI人格のセリフ"
  }
}
```

例（`type="desktop_watch"`）:

```json
{
  "event_id": 123,
  "type": "desktop_watch",
  "data": {
    "system_text": "[desktop_watch] active_app window_title",
    "message": "AI人格のセリフ"
  }
}
```

例（`type="reminder"`）:

```json
{
  "event_id": 123,
  "type": "reminder",
  "data": {
    "reminder_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "hhmm": "09:30",
    "message": "AI人格のセリフ（50文字以内、時刻を含む）"
  }
}
```

視覚（命令）の例:

```json
{
  "event_id": 0,
  "type": "vision.capture_request",
  "data": {
    "request_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "source": "desktop|camera",
    "mode": "still",
    "purpose": "chat|desktop_watch",
    "timeout_ms": 5000
  }
}
```

補足:

- `vision.capture_request` はバッファに保持しない（接続直後のキャッチアップ対象外）
- `reminder` はリアルタイム性が高いため、バッファに保持しない

### クライアント→サーバ（必須: 宛先配信を受ける場合）

接続直後に `hello` を送って `client_id` を登録する。

```json
{
  "type": "hello",
  "client_id": "console-uuid-or-stable-id",
  "caps": ["vision.desktop", "vision.camera"]
}
```

## `/api/control`

Ghostプロセス自体の制御コマンド。

### `POST /api/control`

```json
{
  "action": "shutdown",
  "reason": "任意（省略可）"
}
```

- `action` は現状 `shutdown` のみ許可

### レスポンス

- `204 No Content`

補足:

- 受理後、Ghostは自プロセスへ `SIGTERM` を送って終了する

## 管理API（案）

運用前のため互換は付けない。観測とデバッグを優先する。

- `GET /api/memories/{embedding_preset_id}/events?limit=&offset=&source=`
- `GET /api/memories/{embedding_preset_id}/events/{event_id}`
- `GET /api/memories/{embedding_preset_id}/state?kind=fact|relation|task|summary&limit=&offset=`
- `GET /api/memories/{embedding_preset_id}/state/{kind}/{id}`
- `GET /api/memories/{embedding_preset_id}/state/{kind}/{id}/revisions`
