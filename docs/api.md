# API仕様（パートナー最適 / units前提）

## ベースパス

- APIは **`/api` プレフィックス**を使用する（例: `/api/chat`）

## 認証

- `Authorization: Bearer <TOKEN>`（固定トークン）
- トークン管理は `settings.db` の `global_settings.token` を正とする（初回のみTOMLから投入してもよい）
- `token` は `/api/settings` では更新しない（変更する場合は `settings.db` を編集して再起動）

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

SSEの `event` 名は固定し、`data` は JSON のみとする。

```text
event: token
data: {"text":"..."}

event: done
data: {"episode_unit_id":12345,"reply_text":"...","usage":{...}}

event: error
data: {"message":"...","code":"..."}
```

### サーバ内部フロー（同期）

1. 画像要約（`images` がある場合）
2. Schedulerで **MemoryPack** を生成
3. LLMへ `system_prompt + memorypack + user_text` を注入（MemoryPack内に persona/contract を含む）
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
  "body": "string",
  "images": [
    {"type": "desktop_capture", "base64": "..."},
    {"type": "camera_capture", "base64": "..."}
  ]
}
```

### Response

```json
{ "unit_id": 23456 }
```

- 保存は `units(kind=EPISODE, source=notification)` + `payload_episode.user_text` に本文を入れ、必要なら `context_note` に構造化JSONを入れる
- `images` がある場合は `payload_episode.image_summary` に要約を保存する

## `/api/meta_request`

### Request

```json
{
  "memory_id": "default",
  "instruction": "string",
  "payload_text": "string",
  "images": [
    {"type": "desktop_capture", "base64": "..."},
    {"type": "camera_capture", "base64": "..."}
  ]
}
```

### Response

```json
{ "unit_id": 34567 }
```

## 管理API

以下を提供する。

- **閲覧**
  - `GET /api/memories/{memory_id}/units?kind=&state=&limit=&offset=`
  - `GET /api/memories/{memory_id}/units/{unit_id}`
- **メタ更新（版管理）**
  - `PATCH /api/memories/{memory_id}/units/{unit_id}`（`pin/sensitivity/state/topic_tags` など）

### `topic_tags` の表現（必須）

- `topic_tags` は **JSON array文字列**で固定（例: `["仕事","読書"]`）
- 保存時に正規化（NFKC + 重複除去 + ソート）して `payload_hash` を安定させる

### Worker と `memory_id`（必須）

- `jobs` は `memory_<memory_id>.db` に保存されるため、Worker は **`memory_id` ごとに起動**する
- persona/contract は **settings 側のプロンプトプリセット**として管理し、`memory_id`（記憶DB）とは独立する（切替は `/api/settings`）

- **ジョブ投入**
  - `POST /api/memories/{memory_id}/jobs/weekly_summary`（週次サマリ生成のenqueue）

## 付加API

パートナー最適コアではないが、現行実装に含まれる。

- `/api/capture`（desktop/camera のキャプチャ保存）
- `/api/settings`（UI向けの設定取得/更新）
- `/api/logs/stream`（WebSocketログ購読）

## `/api/settings`

UI向けの「全設定」取得/更新。

### `GET /api/settings`

#### Response（`FullSettingsResponse`）

```json
{
  "exclude_keywords": ["例:除外したい単語"],
  "reminders_enabled": true,
  "reminders": [
    {"scheduled_at": "2025-12-13T12:34:56+09:00", "content": "string"}
  ],
  "active_llm_preset_id": 1,
  "active_embedding_preset_id": 1,
  "active_system_prompt_preset_id": 1,
  "active_persona_preset_id": 1,
  "active_contract_preset_id": 1,
  "llm_preset": [
    {
      "llm_preset_id": 1,
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
      "embedding_preset_id": 1,
      "embedding_preset_name": "default",
      "embedding_model_api_key": "optional",
      "embedding_model": "string",
      "embedding_base_url": "optional",
      "embedding_dimension": 1536,
      "similar_episodes_limit": 10
    }
  ],
  "system_prompt_preset": [
    {
      "system_prompt_preset_id": 1,
      "system_prompt_preset_name": "default",
      "system_prompt": "string"
    }
  ],
  "persona_preset": [
    {
      "persona_preset_id": 1,
      "persona_preset_name": "default",
      "persona_text": "string"
    }
  ],
  "contract_preset": [
    {
      "contract_preset_id": 1,
      "contract_preset_name": "default",
      "contract_text": "string"
    }
  ]
}
```

- `scheduled_at` はISO 8601のdatetime（Pydanticがパース可能な形式）で返す

### `POST /api/settings`

全設定をまとめて更新（共通設定 + プリセット一覧 + `active_*_preset_id` を更新する）。

#### Request（`FullSettingsUpdateRequest`）

```json
{
  "exclude_keywords": ["string"],
  "reminders_enabled": true,
  "reminders": [
    {"scheduled_at": "2025-12-13T12:34:56+09:00", "content": "string"}
  ],
  "active_llm_preset_id": 1,
  "active_embedding_preset_id": 1,
  "active_system_prompt_preset_id": 1,
  "active_persona_preset_id": 1,
  "active_contract_preset_id": 1,
  "llm_preset": [
    {
      "llm_preset_id": 1,
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
      "embedding_preset_id": 1,
      "embedding_preset_name": "default",
      "embedding_model_api_key": "optional",
      "embedding_model": "string",
      "embedding_base_url": "optional",
      "embedding_dimension": 1536,
      "similar_episodes_limit": 10
    }
  ],
  "system_prompt_preset": [
    {
      "system_prompt_preset_id": 1,
      "system_prompt_preset_name": "default",
      "system_prompt": "string"
    }
  ],
  "persona_preset": [
    {
      "persona_preset_id": 1,
      "persona_preset_name": "default",
      "persona_text": "string"
    }
  ],
  "contract_preset": [
    {
      "contract_preset_id": 1,
      "contract_preset_name": "default",
      "contract_text": "string"
    }
  ]
}
```

#### Response

- `GET /api/settings` と同一（更新後の最新状態を返す）

#### 注意点（実装仕様）

- `llm_preset` / `embedding_preset` / `system_prompt_preset` / `persona_preset` / `contract_preset` は「配列」で、**複数件を一括更新**する（`*_preset_id` が未存在の場合は `400`）
- `reminders` は **全置き換え**（既存は削除されIDは作り直される）
- `active_*_preset_id` は **更新後に参照可能なID**である必要がある（未存在は `400`）
- `active_embedding_preset_id` で選択される `embedding_preset_name` は `memory_id` 扱いで、変更時はメモリDB初期化を検証する（失敗時 `400`）
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

#### Response（`CaptureResponse`）

```json
{ "episode_id": 12345, "stored": true }
```

## `/api/logs/stream`（WebSocket）

サーバログの購読（テキストフレームでJSONをpush）。

- URL: `ws(s)://<host>/api/logs/stream`
- 認証: `Authorization: Bearer <TOKEN>`（HTTPヘッダ）

### メッセージ形式

接続直後に直近最大500件のバッファを送信し、その後も新規ログを随時pushする。

```json
{"ts":"2025-12-13T10:00:00+00:00","level":"INFO","logger":"cocoro_ghost.main","msg":"string"}
```

- `ts`: UTCのISO 8601
- `msg`: 改行はスペースに置換される

## `/api/health`

ヘルスチェック。

### `GET /api/health`

```json
{ "status": "healthy" }
```
