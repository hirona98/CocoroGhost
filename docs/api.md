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
  "embedding_preset_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "user_text": "string",
  "images": [
    {"type": "image", "base64": "..."}
  ],
  "client_context": {
    "active_app": "string",
    "window_title": "string",
    "locale": "ja-JP"
  }
}
```

- `embedding_preset_id` は必須
- `embedding_preset_id` は embedding_presets.id（UUID）を想定。埋め込み次元が一致しないDBは初期化に失敗するため、次元一致を前提に指定する
- 注意: `jobs` は `memory_<embedding_preset_id>.db` に作られるが、内蔵Workerの処理対象は `active_embedding_preset_id`（アクティブな `embedding_preset_id`）のみ。非アクティブ `embedding_preset_id` のジョブは処理されない。
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
3. Schedulerで **MemoryPack** を生成（検索結果を `<<<COCORO_GHOST_SECTION:EPISODE_EVIDENCE>>>` に含む）
4. LLMへ system（guard + PERSONA_ANCHOR〔persona_text + addon_text を連結〕 + persona_affect_trailer_prompt）を渡し、conversation は直近会話（max_turns_window）+ `<<INTERNAL_CONTEXT>>`（MemoryPack）+ user_text を渡す
5. 返答をSSEで配信（返答末尾の内部JSON＝persona_affect trailer はサーバ側で回収し、SSEには流さない）
6. `units(kind=EPISODE)` + `payload_episode` を **RAW** で保存
7. Worker用ジョブを enqueue（reflection/extraction/embedding等）

## `/api/persona_mood`（デバッグ）

persona_mood（AI人格の機嫌）関連の数値を **UIから参照/変更**するためのデバッグ用API。

- **永続化しない**（DB/settings.db に保存しない）
- 反映は **同一プロセス内**のみ（プロセスを跨ぐ構成ではプロセスごとに状態が分離される）
- 認証は他の `/api/*` と同様に `Authorization: Bearer <TOKEN>`

### `GET /api/persona_mood`

persona_mood の **前回チャットで使った値（last used）** を返す。
（LLMに渡す直前でDBから取得して計算するため、"現在値"という概念はない）

- `PUT /api/persona_mood` で override を設定しても、**会話（/api/chat）が走るまでは** last used は更新されない

#### Response（JSON）

システムが実際に利用する persona_mood（有効値）を返す。

```json
{
  "label": "neutral",
  "intensity": 0.0,
  "components": {
    "joy": 0.0,
    "sadness": 0.0,
    "anger": 0.0,
    "fear": 0.0
  },
  "response_policy": {
    "cooperation": 1.0,
    "refusal_bias": 0.0,
    "refusal_allowed": false
  }
}
```

### `PUT /api/persona_mood`

in-memory の persona_mood ランタイム状態（次のチャットで有効な値）を設定する

#### Request（JSON）

```json
{
  "label": "anger",
  "intensity": 0.8,
  "components": {
    "joy": 0.0,
    "sadness": 0.1,
    "anger": 0.9,
    "fear": 0.0
  },
  "response_policy": {
    "cooperation": 0.2,
    "refusal_bias": 0.8,
    "refusal_allowed": true
  }
}
```

- `label` は `joy|sadness|anger|fear|neutral` のいずれか
- `intensity` は 0..1
- `components` は `joy/sadness/anger/fear` を **すべて指定**（0..1）
- `response_policy` は `cooperation/refusal_bias/refusal_allowed` を **すべて指定**

#### Response

`GET /api/persona_mood` と同形式（有効値を返す）。

### `DELETE /api/persona_mood`

in-memory の persona_mood ランタイム状態（override）を解除し、自然計算（DBからの同期計算）に戻す。

#### Response

`GET /api/persona_mood` と同形式（解除後の有効値を返す）。

## `/api/v2/notification`

### Request（JSON）

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

### Response

- `204 No Content`

### 例（cURL）

```bash
curl -X POST http://127.0.0.1:55601/api/v2/notification \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"source_system":"MyApp","text":"処理完了","images":["data:image/jpeg;base64,..."]}'
```

```bash
curl -X POST http://127.0.0.1:55601/api/v2/notification \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"source_system":"MyApp","text":"結果","images":["data:image/jpeg;base64,...","data:image/png;base64,..."]}'
```

### 例（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:55601/api/v2/notification" `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{ Authorization = "Bearer <TOKEN>" } `
  -Body '{"source_system":"MyApp","text":"結果","images":["data:image/jpeg;base64,...","data:image/png;base64,..."]}'
```

- HTTPレスポンスは先に返り、AI人格のセリフ（`data.message`）は `/api/events/stream` で後から届く
- 保存は `units(kind=EPISODE, source=notification)` + `payload_episode.user_text` に本文を入れ、必要なら `context_note` に構造化JSONを入れる
- `images` がある場合は `payload_episode.image_summary` に要約を保存する


## `/api/v2/meta-request`

### Request（JSON）

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

### Response

- `204 No Content`

### 例（cURL）

```bash
curl -X POST http://127.0.0.1:55601/api/v2/meta-request \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"instruction":"これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。","payload_text":"～ニュース内容～"}'
```

### 例（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:55601/api/v2/meta-request" `
  -ContentType "application/json; charset=utf-8" `
  -Headers @{ Authorization = "Bearer <TOKEN>" } `
  -Body '{"instruction":"これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。","payload_text":"～ニュース内容～"}'
```

- HTTPレスポンスは先に返り、AI人格のセリフ（`data.message`）は `/api/events/stream` で後から届く
- `instruction` / `payload_text` は **永続化しない**（生成にのみ利用）
- 生成結果は「ユーザーに話しかけるための本文」であり、`units(kind=EPISODE, source=meta-request)` の `payload_episode.reply_text` に保存する

## 管理API

以下を提供する。

- **閲覧**
  - `GET /api/memories/{embedding_preset_id}/units?kind=&state=&limit=&offset=`
  - `GET /api/memories/{embedding_preset_id}/units/{unit_id}`
- **メタ更新（版管理）**
  - `PATCH /api/memories/{embedding_preset_id}/units/{unit_id}`（`pin/sensitivity/state/topic_tags/confidence/salience` など）

### `topic_tags` の表現

- `topic_tags` は **JSON array文字列**（例: `["仕事","読書"]`）
- 保存時に正規化（NFKC + 重複除去 + ソート）して `payload_hash` を安定させる

### Worker と `embedding_preset_id`

- `jobs` は `memory_<embedding_preset_id>.db` に保存されるため、Worker は **アクティブな `embedding_preset_id`（= `active_embedding_preset_id`）** を対象に処理する（内蔵Worker）。
- PersonaPreset/AddonPreset は **settings 側のプロンプトプリセット**として管理し、注入時は persona_text + addon_text を PERSONA_ANCHOR として連結する。`embedding_preset_id`（記憶DB）とは独立する（切替は `/api/settings`）

補足:
- `jobs` は内部用のキューであり、外部から任意のジョブを投入する汎用APIは提供しない。

## 付加API

現行実装に含まれる。

- `/api/settings`（UI向けの設定取得/更新）
- `/api/logs/stream`（WebSocketログ購読）
- `/api/events/stream`（WebSocketイベント購読: notification/meta-request）

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
- `exclude_keywords` は現状未使用（将来の入力フィルタ用途として予約）

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
- `active_embedding_preset_id` は記憶DB識別子（= `embedding_preset_id`）で、変更時はメモリDB初期化を検証する（失敗時 `400`）
- `max_inject_tokens` / `similar_limit_by_kind` 等の詳細パラメータは現状API外

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
- 目的: `POST /api/v2/notification` / `POST /api/v2/meta-request` を受信したとき、接続中クライアントへ即時にイベントを配信する
- 挙動: 接続直後に最大200件のバッファ済みイベントを送信し、その後は新規イベントをリアルタイムでpushする

### Event payload（JSON text）

サーバは WebSocket の `text` として、以下形式の JSON を送信する。

```json
{
  "unit_id": 12345,
  "type": "notification|meta-request",
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
    "message": "AIAI人格のセリフ"
  }
}

{
  "unit_id": 12345,
  "type": "meta-request",
  "data": {
    "message": "AI人格のセリフ",
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
