# cocoro_ghost API 仕様（パートナー最適 / units + MemoryPack）

本ドキュメントは「AIパートナー最適」仕様に合わせた API の入口です。詳細は `docs/partner_spec/api.md` を参照してください。

## 共通事項

- ベースパス: 実装は既存互換のため `/api` プレフィックスを維持してよい（例: `/api/chat`）
- 認証: `Authorization: Bearer <TOKEN>`（固定トークン）
- ストレージ: `settings.db` + `memory_<memory_id>.db`

## 1. `/chat`（SSE）

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

### SSE Events（推奨）

```text
event: token
data: {"text":"..."}

event: done
data: {"episode_unit_id":12345,"reply_text":"...","usage":{...}}

event: error
data: {"message":"...","code":"..."}
```

### サーバ内部フロー（概要）

- Scheduler が MemoryPack を編成し、`persona/contract/facts/summaries/loops/episodes` を規定順で注入する
- 保存は `units(kind=EPISODE)` + `payload_episode` を RAW で行い、派生処理は `jobs` に積む

## 2. `/notification`

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

## 3. `/meta_request`

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

## 4. 管理API（提案）

メモリ閲覧・編集・ピン留め等。最小案は `docs/partner_spec/api.md` に記載。

