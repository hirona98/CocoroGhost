# API仕様

## ベースパス

- APIは **`/api` プレフィックス**を使用する（例: `/api/chat`）

## 認証

- `Authorization: Bearer <TOKEN>`
- トークン管理は `settings.db` の `global_settings.token` を正とする（初回のみTOMLから投入してもよい）
- `token` は `/api/settings` では更新しない（変更する場合は `settings.db` を編集して再起動）

## `/api/chat`（SSE）

### Request（JSON）

```json
{
  "memory_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "user_text": "string",
  "images": [
    {"type": "desktop_capture", "base64": "..."},
    {"type": "camera_capture", "base64": "..."}
  ],
  "client_context": {
    "active_app": "string",
    "window_title": "string",
    "locale": "ja-JP"
  }
}
```

- `memory_id` は省略可能（省略時は、`/api/settings` で選択中の `active_embedding_preset_id` を `memory_id` として使用する）
- `memory_id` は embedding_presets.id（UUID）を想定。埋め込み次元が一致しないDBは初期化に失敗するため、次元一致を前提に指定する
- `images` は省略可能。要素は現状 `base64` のみ参照し、`type` は未使用（`base64` が空/不正な要素は無視される）
- `client_context` は省略可能（指定時は `payload_episode.context_note` にJSON文字列として保存される）

### SSE Events

SSEの `event` 名は `token` / `done` / `error` を使用し、`data` は JSON のみとする。

```text
event: token
data: {"text":"..."}

event: done
data: {"episode_unit_id":12345,"reply_text":"...","usage":{...}}

event: error
data: {"message":"...","code":"..."}
```

- `done.usage` は現状 `{}`（予約フィールド）

### サーバ内部フロー（同期）

1. 画像要約（`images` がある場合）
2. Retrieverで文脈考慮型の記憶検索（`docs/retrieval.md`）
3. Schedulerで **MemoryPack** を生成（検索結果を `[EPISODE_EVIDENCE]` に含む）
4. LLMへ `memorypack + mood_trailer_prompt` を system に注入し、conversation には直近会話（max_turns_window）+ user_text を渡す（MemoryPack内に persona/addon を含む）
5. 返答をSSEで配信（返答末尾の内部JSON＝mood trailer はサーバ側で回収し、SSEには流さない）
6. `units(kind=EPISODE)` + `payload_episode` を **RAW** で保存
7. Worker用ジョブを enqueue（reflection/extraction/embedding等）

## `/api/mood`（デバッグ）

mood（パートナーの機嫌）関連の数値を **UIから参照/変更**するためのデバッグ用API。

- **永続化しない**（DB/settings.db に保存しない）
- 反映は **同一プロセス内**のみ（プロセスを跨ぐ構成ではプロセスごとに状態が分離される）
- 認証は他の `/api/*` と同様に `Authorization: Bearer <TOKEN>`

### `GET /api/mood`

現在の mood を返す。

- query
  - `scan_limit`（任意）: DB走査件数（既定 500、範囲は内部で 50..2000 に丸める）
  - `include_computed`（任意）: `computed` を含めるか（既定 true）

#### Response（JSON）

- `computed`: DBのエピソードから「重要度×時間減衰」で計算した mood（取得に失敗した場合は `null`）
- `override`: デバッグ用の in-memory 上書き（無ければ `null`）
- `effective`: 実際にシステムが利用する mood（`computed` に `override` を適用）

### `PUT /api/mood/override`

in-memory の mood override を設定する（**部分更新可**）。

#### Request（JSON）

```json
{
  "label": "anger",
  "intensity": 0.8,
  "components": {"anger": 0.9},
  "policy": {"refusal_allowed": true}
}
```

- `label` は `joy|sadness|anger|fear|neutral` のいずれか
- `components` は `joy/sadness/anger/fear` を任意指定（0..1）
- `policy` は `cooperation/refusal_bias/refusal_allowed` を任意指定

#### Response

`GET /api/mood` と同形式。

### `DELETE /api/mood/override`

override を解除する。

#### Response

`GET /api/mood` と同形式。

## `/api/v1/notification`

### Request（JSON）

```json
{
  "from": "アプリ名",
  "message": "通知メッセージ",
  "images": [
    "data:image/jpeg;base64,/9j/4AAQ...",
    "data:image/png;base64,iVBORw0KGgo..."
  ]
}
```

- `images` は省略可能（最大5枚）
- `images` の要素は `data:image/*;base64,...` 形式の Data URI

### Response

- `204 No Content`

### 例（cURL）

```bash
curl -X POST http://127.0.0.1:55601/api/v1/notification \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"from":"MyApp","message":"処理完了","images":["data:image/jpeg;base64,..."]}'
```

```bash
curl -X POST http://127.0.0.1:55601/api/v1/notification \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"from":"MyApp","message":"結果","images":["data:image/jpeg;base64,...","data:image/png;base64,..."]}'
```

### 例（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:55601/api/v1/notification" `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{ Authorization = "Bearer <TOKEN>" } `
  -Body '{"from":"MyApp","message":"結果","images":["data:image/jpeg;base64,...","data:image/png;base64,..."]}'
```

- HTTPレスポンスは先に返り、パートナーのセリフ（`data.message`）は `/api/events/stream` で後から届く
- 保存は `units(kind=EPISODE, source=notification)` + `payload_episode.user_text` に本文を入れ、必要なら `context_note` に構造化JSONを入れる
- `images` がある場合は `payload_episode.image_summary` に要約を保存する


## `/api/v1/meta_request`

### Request（JSON）

```json
{
  "prompt": "任意のプロンプトやメッセージ",
  "images": [
    "data:image/jpeg;base64,/9j/4AAQ...",
    "data:image/png;base64,iVBORw0KGgo..."
  ]
}
```

- `images` は省略可能（最大5枚）
- `images` の要素は `data:image/*;base64,...` 形式の Data URI

### Response

- `204 No Content`

### 例（cURL）

```bash
curl -X POST http://127.0.0.1:55601/api/v1/meta_request \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"prompt":"これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。：～ニュース内容～"}'
```

### 例（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:55601/api/v1/meta_request" `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{ Authorization = "Bearer <TOKEN>" } `
  -Body '{"prompt":"これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。：～ニュース内容～"}'
```

- HTTPレスポンスは先に返り、パートナーのセリフ（`data.message`）は `/api/events/stream` で後から届く
- `prompt` は **永続化しない**（生成にのみ利用）
- 生成結果は「ユーザーに話しかけるための本文」であり、`units(kind=EPISODE, source=meta_request)` の `payload_episode.reply_text` に保存する

## 管理API

以下を提供する。

- **閲覧**
  - `GET /api/memories/{memory_id}/units?kind=&state=&limit=&offset=`
  - `GET /api/memories/{memory_id}/units/{unit_id}`
- **メタ更新（版管理）**
  - `PATCH /api/memories/{memory_id}/units/{unit_id}`（`pin/sensitivity/state/topic_tags/confidence/salience` など）

### `topic_tags` の表現

- `topic_tags` は **JSON array文字列**（例: `["仕事","読書"]`）
- 保存時に正規化（NFKC + 重複除去 + ソート）して `payload_hash` を安定させる

### Worker と `memory_id`

- `jobs` は `memory_<memory_id>.db` に保存されるため、Worker は **アクティブな `memory_id`（= `active_embedding_preset_id`）** を対象に処理する（内蔵Worker）。
- persona/addon は **settings 側のプロンプトプリセット**として管理し、`memory_id`（記憶DB）とは独立する（切替は `/api/settings`）

補足:
- `jobs` は内部用のキューであり、外部から任意のジョブを投入する汎用APIは提供しない。

## 付加API

現行実装に含まれる。

- `/api/capture`（desktop/camera のキャプチャ保存）
- `/api/settings`（UI向けの設定取得/更新）
- `/api/logs/stream`（WebSocketログ購読）
- `/api/events/stream`（WebSocketイベント購読: notification/meta_request）

## `/api/settings`

UI向けの「全設定」取得/更新。

### `GET /api/settings`

#### Response（`FullSettingsResponse`）

```json
{
  "exclude_keywords": ["例:除外したい単語"],
  "memory_enabled": true,
  "reminders_enabled": true,
  "reminders": [
    {"scheduled_at": "2025-12-13T12:34:56+09:00", "content": "string"}
  ],
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
      "similar_episodes_limit": 10
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

- `scheduled_at` はISO 8601のdatetime（Pydanticがパース可能な形式）で返す
- `memory_enabled` は「記憶機能を使うか」を示す設定値

### `PUT /api/settings`

全設定をまとめて確定（共通設定 + プリセット一覧 + `active_*_preset_id`）。

このAPIは「設定画面の OK/適用」向けに **全置換コミット** として動作する:

- `*_preset` 各配列は **最終的に残したいプリセットの完成形**を送る
- サーバ側は `*_preset_id`（UUID）で upsert する（未存在なら作成、存在すれば更新）
- リクエストに含まれない既存プリセットは **削除せず `archived=true` にする**
- `GET /api/settings` は `archived=false` のもののみ返す

#### Request（`FullSettingsUpdateRequest`）

```json
{
  "exclude_keywords": ["string"],
  "memory_enabled": true,
  "reminders_enabled": true,
  "reminders": [
    {"scheduled_at": "2025-12-13T12:34:56+09:00", "content": "string"}
  ],
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
      "similar_episodes_limit": 10
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

#### Response

- `GET /api/settings` と同一（更新後の状態を返す）

#### 注意点（実装仕様）

- `llm_preset` / `embedding_preset` / `persona_preset` / `addon_preset` は「配列」で、**複数件を一括確定**する（全置換コミット）
- `reminders` は **全置き換え**（既存は削除されIDは作り直される）
- 各配列内で `*_preset_id` が重複している場合は `400`
- `active_*_preset_id` は **対応する配列に含まれるID**である必要がある（未存在/アーカイブは `400`）
- `active_embedding_preset_id` は `memory_id` 扱いで、変更時はメモリDB初期化を検証する（失敗時 `400`）
- `max_inject_tokens` / `similar_limit_by_kind` 等の詳細パラメータは現状API外

## `/api/capture`

キャプチャ画像をUnit(Episode)として保存し、派生ジョブをenqueueする。

### `POST /api/capture`

#### Request（`CaptureRequest`）

```json
{
  "capture_type": "desktop",
  "image_base64": "string",
  "context_text": "optional"
}
```

- `capture_type`: `"desktop"` または `"camera"`
- `image_base64`: 画像のbase64（data URLヘッダ無しの想定）
- `context_text`: 保存時の `payload_episode.user_text` に入る（省略可）

- 除外判定: `context_text` が `exclude_keywords` のいずれかにマッチする場合、保存せずに `{"episode_id":-1,"stored":false}` を返す。`exclude_keywords` は正規表現メタ文字（例: `.*`）を含むと正規表現として評価され、それ以外は部分一致で判定する。
- `capture_type` は現状厳密バリデーションしない（`"desktop"` 以外は `"camera"` 扱い）

#### Response（`CaptureResponse`）

```json
{ "episode_id": 12345, "stored": true }
```

## `/api/logs/stream`（WebSocket）

サーバログの購読（テキストフレームでJSONをpush）。

- URL: `ws(s)://<host>/api/logs/stream`
- 認証: `Authorization: Bearer <TOKEN>`

### メッセージ形式

接続直後に直近最大500件のバッファを送信し、その後も新規ログを随時pushする。

```json
{"ts":"2025-12-13T10:00:00+00:00","level":"INFO","logger":"cocoro_ghost.main","msg":"string"}
```

- `ts`: UTCのISO 8601
- `msg`: 改行はスペースに置換される


## `/api/events/stream`（WebSocket）

- URL: `ws(s)://<host>/api/events/stream`
- 認証: `Authorization: Bearer <TOKEN>`
- 目的: `POST /api/v1/notification` / `POST /api/v1/meta_request` を受信したとき、接続中クライアントへ即時にイベントを配信する
- 挙動: 接続直後に最大200件のバッファ済みイベントを送信し、その後は新規イベントをリアルタイムでpushする

### Event payload（JSON text）

サーバは WebSocket の `text` として、以下形式の JSON を送信する。

```json
{
  "unit_id": 12345,
  "type": "notification|meta_request",
  "data": {
    "system_text": "string",
    "message": "string"
  }
}
```

例）
```json
{
  "unit_id": 12345,
  "type": "notification",
  "data": {
    "system_text": "[notificationのfrom] notificationのmessage",
    "message": "AIパートナーのセリフ"
  }
}

{
  "unit_id": 12345,
  "type": "meta_request",
  "data": {
    "message": "AIパートナーのセリフ"
  }
}
```

- 認証: `Authorization: Bearer <TOKEN>`（HTTPヘッダ）


## `/api/health`

ヘルスチェック。

### `GET /api/health`

```json
{ "status": "healthy" }
```

## `/`（参考）

APIベースパス外だが、簡易確認用に提供している。

### `GET /`

```json
{ "message": "CocoroGhost API is running" }
```
