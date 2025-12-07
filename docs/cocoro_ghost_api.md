# cocoro_ghost API 仕様（LiteLLM Response API 版）

LiteLLM の Response API を前提にした cocoro_ghost の API 仕様。LLM 呼び出し結果は OpenAI 互換の Response 形式で返却し、従来のプレーン文字列レスポンスは提供しない。

## 共通事項

- ベース URL: 例 `https://example.com/api/ghost`
- 認証:
  - すべてのエンドポイントで固定トークン認証。
  - HTTP ヘッダ `Authorization: Bearer <TOKEN>` が必須。
- リクエスト/レスポンス: `Content-Type: application/json`
- 日時: ISO 8601（UTC）
- LLM レスポンス: OpenAI 互換の `chat.completion` Response オブジェクト（例は後述）。`return_response_object=True` で得られる形をそのまま返す。
- エラー: LLM 呼び出し失敗時は 5xx。業務エラーは 4xx/5xx を使い、成功時にエラー文を埋め込む動作は行わない。

### LLM レスポンス例（非ストリーム）

```json
{
  "id": "cmpl-xyz",
  "object": "chat.completion",
  "created": 1730188860,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "今日は少し疲れてるみたい。無理しないでね。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 45,
    "total_tokens": 168
  }
}
```

## 1. 通常会話 API `/chat`（SSE ストリーミングのみ）

ユーザーとの会話をストリーミングで返しつつ、完了時にエピソードを作成する。

- メソッド: `POST`
- パス: `/api/chat`
- 応答: `text/event-stream`（SSE）

### リクエストボディ

```json
{
  "user_id": "default",
  "text": "今日はちょっと疲れた",
  "context_hint": "evening",
  "image_base64": null
}
```

- `user_id`: 将来拡張用。初期は `"default"` のみ。
- `text`: ユーザー発話。
- `context_hint`: 任意。状況ヒント。
- `image_base64`: 任意。BASE64 画像。

### ストリーミング仕様（SSE）

- 同期レスポンスは廃止。常に SSE で返す。
- フォーマット:
  - 生成中: `data: {"type":"token","delta":"..."}\n\n`
  - 完了: `data: {"type":"done","episode_id":123,"reply_text":"..."}\n\n`
  - エラー: `data: {"type":"error","message":"..."}\n\n`
- クライアントは EventSource などで購読し、`done` イベントで最終テキストと episode_id を取得する。

## 2. 通知受信 API `/notification`

外部通知を受け取り、ユーザーへ伝えるメッセージとエピソードを生成する。

- メソッド: `POST`
- パス: `/api/notification`

### リクエストボディ

```json
{
  "source_system": "mailer",
  "title": "XXさんからのメール",
  "body": "～メール本文サマリ～",
  "image_base64": null
}
```

- `source_system`: 通知元。
- `title`: 通知タイトル。
- `body`: 通知本文サマリ。
- `image_base64`: 任意。BASE64 画像。

### レスポンスボディ

```json
{
  "episode_id": 124,
  "llm_response": {
    "...": "OpenAI 互換の chat.completion Response（ユーザーに伝えるメッセージ）"
  }
}
```

## 3. メタ要求 API `/meta_request`

キャラクターへのメタ指示と本文を渡し、ユーザー向け発話を生成する。

- メソッド: `POST`
- パス: `/api/meta_request`

### リクエストボディ

```json
{
  "instruction": "これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。",
  "payload_text": "～ニュース内容～",
  "image_base64": null
}
```

- `instruction`: 指示文。
- `payload_text`: 対象本文。
- `image_base64`: 任意。BASE64 画像。

### レスポンスボディ

```json
{
  "episode_id": 125,
  "llm_response": {
    "...": "OpenAI 互換の chat.completion Response（ユーザーへ話す内容）"
  }
}
```

## 4. 画像キャプチャ API `/capture`

デスクトップ/カメラの画像を受け取り、要約・内的思考・エピソードを生成する。

- メソッド: `POST`
- パス: `/api/capture`

### リクエストボディ

```json
{
  "capture_type": "desktop",
  "image_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
  "context_text": "ブラウザで技術記事を読んでいる"
}
```

- `capture_type`: `desktop` or `camera`。
- `image_base64`: BASE64 画像（必須）。
- `context_text`: 任意。文脈補足。

### レスポンスボディ

```json
{
  "episode_id": 126,
  "stored": true
}
```

- 画像要約・反省（reflection）など内部で生成する LLM 呼び出しは Response 形式で保持するが、API レスポンスとしてはエピソード ID のみ返す。

## 5. 設定管理 API `/settings`

共通設定とアクティブなプリセットを取得する。

- メソッド: `GET`, `POST`
- パス: `/api/settings`

### レスポンスボディ

```json
{
  "exclude_keywords": ["パスワード", "銀行"],
  "llm_preset": [
    {
      "llm_preset_id": 1,
      "llm_preset_name": "default",
      "system_prompt": "キャラクターのシステムプロンプト...",
      "llm_api_key": "sk-...",
      "llm_model": "gpt-4o",
      "reasoning_effort": null,
      "llm_base_url": null,
      "max_turns_window": 50,
      "max_tokens": 4096,
      "image_model_api_key": "sk-...-image",
      "image_model": "gpt-4o-mini",
      "image_llm_base_url": null,
      "max_tokens_vision": 4096,
      "image_timeout_seconds": 60
    }
  ],
  "embedding_preset": [
    {
      "embedding_preset_id": 1,
      "embedding_preset_name": "default",
      "embedding_model_api_key": "sk-...-embedding",
      "embedding_model": "text-embedding-3-small",
      "embedding_base_url": null,
      "embedding_dimension": 1536,
      "similar_episodes_limit": 5
    }
  ]
}
```

## 6. リアルタイムログ WebSocket `/api/logs/stream`

cocoro_ghost のログをリアルタイムで取得する。接続時に直近 500 件を送信し、その後は新しいログを随時送信する。

- メソッド: `GET`（WebSocket）
- パス: `/api/logs/stream`
- 認証: `Authorization: Bearer <TOKEN>`

### 接続後の流れ

1. 接続時に直近 500 件のログを送信。
2. 以降は新規ログを 1 件ずつ送信。
3. クライアント送信は不要（受信専用）。

### メッセージ形式（テキスト）

```json
{"ts": "2025-01-01T12:00:00+00:00", "level": "INFO", "logger": "cocoro_ghost.chat", "msg": "LLM呼び出し開始"}
```
