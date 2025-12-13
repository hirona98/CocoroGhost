# API仕様（パートナー最適 / units前提）

## 認証

- `Authorization: Bearer <TOKEN>`（固定トークン）
- トークン管理は `settings.db` の `global_settings.token` を正とする（初回のみTOMLから投入してもよい）

## `/chat`（SSE）

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

## `/notification`

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

- 保存は `units(kind=EPISODE, source=notification)` + `payload_episode.context_note` 等に格納する

## `/meta_request`

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
  - `GET /memories/{memory_id}/units?kind=&state=&limit=&offset=`
  - `GET /memories/{memory_id}/units/{unit_id}`
- **メタ更新（版管理）**
  - `PATCH /memories/{memory_id}/units/{unit_id}`（`pin/sensitivity/state/topic_tags` など）
- **persona/contractの切替**
  - `POST /memories/{memory_id}/persona`（新規追加・active化）
  - `POST /memories/{memory_id}/contract`（新規追加・active化）

