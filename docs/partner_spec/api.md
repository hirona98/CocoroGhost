# API仕様（パートナー最適 / units前提）

## ベースパス

- 実装は既存互換のため **`/api` プレフィックス**を使用する（例: `/api/chat`）

## 認証

- `Authorization: Bearer <TOKEN>`（固定トークン）
- トークン管理は `settings.db` の `global_settings.token` を正とする（初回のみTOMLから投入してもよい）

## `/api/chat`（SSE）

### Request（JSON）

```json
{
  "memory_id": "default",
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

### SSE Events

推奨：SSEの `event` 名を固定し、`data` は JSON のみにする。

```text
event: token
data: {"text":"..."}

event: done
data: {"episode_unit_id":12345,"reply_text":"...","usage":{...}}

event: error
data: {"message":"...","code":"..."}
```

### サーバ内部フロー（同期）

1. （任意）画像要約（vision）
2. Schedulerで **MemoryPack** を生成
3. LLMへ `system_prompt + persona + contract + memorypack + user_text` を注入
4. 返答をSSEで配信
5. `units(kind=EPISODE)` + `payload_episode` を **RAW** で保存
6. Worker用ジョブを enqueue（reflection/extraction/embedding等）

## `/api/notification`

### Request

```json
{
  "memory_id": "default",
  "source_system": "gmail",
  "title": "string",
  "body": "string"
}
```

### Response

```json
{ "unit_id": 23456 }
```

- 保存は `units(kind=EPISODE, source=notification)` + `payload_episode.user_text` に本文を入れ、必要なら `context_note` に構造化JSONを入れる

## `/api/meta_request`

### Request

```json
{
  "memory_id": "default",
  "instruction": "string",
  "payload_text": "string"
}
```

### Response（提案）

```json
{ "unit_id": 34567 }
```

## 管理API（提案：最小）

実装の都合でパスは調整してよいが、以下の機能は必要。

- **閲覧**
  - `GET /api/memories/{memory_id}/units?kind=&state=&limit=&offset=`
  - `GET /api/memories/{memory_id}/units/{unit_id}`
- **メタ更新（版管理）**
  - `PATCH /api/memories/{memory_id}/units/{unit_id}`（`pin/sensitivity/state/topic_tags` など）
- **persona/contractの切替**
  - `POST /api/memories/{memory_id}/persona`（新規追加・active化）
  - `POST /api/memories/{memory_id}/contract`（新規追加・active化）

- **ジョブ投入（任意）**
  - `POST /api/memories/{memory_id}/jobs/weekly_summary`（週次サマリ生成のenqueue）

## 付加API（既存互換）

パートナー最適コアではないが、現行実装に含まれる。

- `/api/capture`（desktop/camera のキャプチャ保存）
- `/api/settings`（UI向けの設定取得/更新）
- `/api/logs/stream`（WebSocketログ購読）
