# cocoro_ghost API 仕様（初期案）

このドキュメントは、CocoroAI のコアコンポーネントである cocoro_ghost が提供する
REST API の初期仕様をまとめたものです。

## 共通事項

- ベース URL: 例 `https://example.com/api/ghost`
- 認証:
  - すべてのエンドポイントで、固定トークンによる認証を行う。
  - HTTP ヘッダ `Authorization: Bearer <TOKEN>` を必須とする（初期実装想定）。
- リクエスト／レスポンス:
  - `Content-Type: application/json`
  - 日時は ISO 8601（UTC）文字列で扱う。
- エラーレスポンス:
  - `/chat` では LLM レート制限・通信失敗などのエラーが発生した場合、HTTP 200 で `reply_text` にエラーメッセージを入れて返す（フォールバックリトライなし）。
  - `/chat` 以外のエンドポイントで内部エラーが発生した場合は 5xx を返し、特別なフォールバックは行わない。

---

## 1. 通常会話 API `/chat`

ユーザーとの通常のテキスト会話を行い、その結果をエピソードとして記録する。

- メソッド: `POST`
- パス: `/chat`

### リクエストボディ

```json
{
  "user_id": "default",
  "text": "今日はちょっと疲れた",
  "context_hint": "evening",
  "image_base64": null
}
```

- `user_id`: 将来的な拡張用。初期は `"default"` 固定のみを受け付ける。
  将来マルチユーザー対応を行う場合も、既存 API との互換を維持するのではなく、必要に応じて非互換な変更（スキーマ変更など）を行う前提とする。
- `text`: ユーザーの発話内容。
- `context_hint`: 任意。時間帯や状況のヒントがあれば渡す。
- `image_base64`: 任意。BASE64エンコードされた画像データ。

### レスポンスボディ

```json
{
  "reply_text": "今日は少しお疲れみたいだね。何かあった？",
  "episode_id": 123
}
```

- `reply_text`: CocoroAI からユーザーへ返すメッセージ。
- `episode_id`: 作成されたエピソードの ID。

---

## 2. 通知受信 API `/notification`

外部システム（メーラーなど）からの通知を受け取り、ユーザーへ伝えるメッセージと
エピソードを生成する。

- メソッド: `POST`
- パス: `/notification`

### リクエストボディ

```json
{
  "source_system": "mailer",
  "title": "XXさんからのメール",
  "body": "～メール本文サマリ～",
  "image_base64": null
}
```

- `source_system`: 通知元システム（例: `mailer`, `calendar`, `system`）。
- `title`: 通知のタイトルや概要。
- `body`: 通知内容のサマリ。
- `image_base64`: 任意。BASE64エンコードされた画像データ。

### レスポンスボディ

```json
{
  "speak_text": "メーラーから通知がありました。XXさんからメールが来ています。大事そうな内容だね。",
  "episode_id": 124
}
```

- `speak_text`: キャラクターがユーザーに伝えるべきメッセージ。
- `episode_id`: 作成されたエピソードの ID。

---

## 3. メタ要求 API `/meta_request`

キャラクターに対して「こういう説明・振る舞いをしてほしい」というメタレベルの指示を渡し、
ユーザー向けの発話を生成する。

- メソッド: `POST`
- パス: `/meta_request`

### リクエストボディ

```json
{
  "instruction": "これは直近1時間のニュースです。内容をユーザに説明するとともに感想を述べてください。",
  "payload_text": "～ニュース内容～",
  "image_base64": null
}
```

- `instruction`: キャラクターへの指示文。
- `payload_text`: 指示の対象となる本文（ニュース、記事、ログなど）。
- `image_base64`: 任意。BASE64エンコードされた画像データ。

### レスポンスボディ

```json
{
  "speak_text": "ここ1時間でこんなニュースがあったよ。ざっくり言うと……わたしはこう感じた。",
  "episode_id": 125
}
```

- `speak_text`: ユーザーに対して話すべきメッセージ。
- `episode_id`: 作成されたエピソードの ID。

---

## 4. 画像キャプチャ API `/capture`

デスクトップやカメラから取得した画像キャプチャを cocoro_ghost に通知し、
要約・内的思考・エピソード生成を行う。

- メソッド: `POST`
- パス: `/capture`

### リクエストボディ

```json
{
  "capture_type": "desktop",
  "image_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
  "context_text": "ブラウザで技術記事を読んでいる"
}
```

- `capture_type`: `desktop` または `camera`。
- `image_base64`: BASE64エンコードされた画像データ。
- `context_text`: 任意。シーンに関する補足テキスト。

### レスポンスボディ

```json
{
  "episode_id": 126,
  "stored": true
}
```

- `episode_id`: 作成されたエピソードの ID。
- `stored`: エピソードが保存されたかどうか。

---

## 5. エピソード取得 API `/episodes` // FIX: 廃止

記録済みエピソードの一覧を取得する。振り返り UI 等から利用する想定。

- メソッド: `GET`
- パス: `/episodes`
- クエリパラメータ:
  - `limit`: 取得件数（例: 20）
  - `offset`: オフセット（例: 0）

### レスポンスボディ

```json
{
  "episodes": [
    {
      "id": 123,
      "occurred_at": "2025-03-01T10:00:00Z",
      "source": "chat",
      "user_text": "今日はちょっと疲れた",
      "reply_text": "今日は少しお疲れみたいだね。何かあった？",
      "emotion_label": "sadness",
      "salience_score": 0.7
    }
  ]
}
```

- 実際には、要件定義で示したエピソード情報（emotion, topic_tags など）を必要に応じて返す。

---

## 6. 設定管理 API

### 6.1 全設定値取得 `GET /settings`

共通設定と、現在アクティブなプリセットの詳細をまとめて取得する。

- メソッド: `GET`
- パス: `/settings`

#### レスポンスボディ

```json
{
  "exclude_keywords": ["パスワード", "銀行"],
  "llm_preset": [{
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
  }],
  "embedding_preset": [{
    "embedding_preset_id": 1,
    "embedding_preset_name": "default", // character_preset.memory_id だったもの
    "embedding_model_api_key": "sk-...-embedding",
    "embedding_model": "text-embedding-3-small",
    "embedding_base_url": null,
    "embedding_dimension": 1536,
    "similar_episodes_limit": 5,
  }],
  // "character_preset": { // FIX:廃止
  //   "id": 1, // FIX:廃止
  //   "name": "default", // FIX:廃止
  //   "memory_id": "default" // FIX:移動
  // }
}
```

※ APIキーもレスポンスに含まれる（API以外の管理インタフェースなし）。

### 6.2 全設定値設定 `POST /settings` // FIX: 追加



### 6.2 共通設定更新 `PATCH /settings` // FIX:廃止

共通設定（現在は `exclude_keywords` のみ）を更新する。

- メソッド: `PATCH`
- パス: `/settings`

#### リクエストボディ

```json
{
  "exclude_keywords": ["秘密", "パスワード"]
}
```

#### レスポンスボディ

```json
{
  "exclude_keywords": ["秘密", "パスワード"],
  "active_llm_preset_id": 1,
  "active_character_preset_id": 1
}
```

---

## 7. LLMプリセット API // FIX:廃止

### 7.1 プリセット一覧取得 `GET /llm-presets`

保存されている LLM プリセット一覧と、現在アクティブなプリセット ID を取得する。

- メソッド: `GET`
- パス: `/llm-presets`

#### レスポンスボディ

```json
{
  "presets": [
    { "id": 1, "name": "default", "llm_model": "gpt-4o" },
    { "id": 2, "name": "fast", "llm_model": "gpt-4o-mini" }
  ],
  "active_id": 1
}
```

### 7.2 プリセット作成 `POST /llm-presets`

新しい LLM プリセットを作成する。

- メソッド: `POST`
- パス: `/llm-presets`

#### リクエストボディ

```json
{
  "name": "fast",
  "llm_api_key": "sk-...",
  "llm_model": "gpt-4o-mini",
  "llm_base_url": null,
  "reasoning_effort": null,
  "max_turns_window": 50,
  "max_tokens_vision": 2048,
  "max_tokens": 2048,
  "embedding_model": "text-embedding-3-small",
  "embedding_api_key": "sk-...-embedding",
  "embedding_base_url": null,
  "embedding_dimension": 1536,
  "image_model": "gpt-4o-mini",
  "image_model_api_key": "sk-...-image",
  "image_llm_base_url": null,
  "image_timeout_seconds": 60,
  "similar_episodes_limit": 5
}
```

#### レスポンスボディ

```json
{
  "id": 2,
  "name": "fast",
  "llm_api_key": "sk-...",
  "llm_model": "gpt-4o-mini",
  "llm_base_url": null,
  "reasoning_effort": null,
  "max_turns_window": 50,
  "max_tokens_vision": 2048,
  "max_tokens": 2048,
  "embedding_model": "text-embedding-3-small",
  "embedding_api_key": "sk-...-embedding",
  "embedding_base_url": null,
  "embedding_dimension": 1536,
  "image_model": "gpt-4o-mini",
  "image_model_api_key": "sk-...-image",
  "image_llm_base_url": null,
  "image_timeout_seconds": 60,
  "similar_episodes_limit": 5
}
```

### 7.3 プリセット取得 `GET /llm-presets/{id}`

- メソッド: `GET`
- パス: `/llm-presets/{id}`

### 7.4 プリセット更新 `PATCH /llm-presets/{id}`

部分更新。指定フィールドのみ反映される。

```json
{
  "llm_model": "gpt-4o",
  "llm_base_url": "https://example.com/v1",
  "max_tokens": 4096
}
```

### 7.5 プリセット削除 `DELETE /llm-presets/{id}`

アクティブなプリセットは削除不可。`400 Bad Request` を返す。

### 7.6 プリセット切り替え `POST /llm-presets/{id}/activate`

指定した LLM プリセットをアクティブ化する。**切り替え後はアプリ再起動が必要**。

```json
{
  "message": "Activated LLM preset 'fast'. Please restart the application.",
  "restart_required": true
}
```

---

## 8. キャラクタープリセット API

### 8.1 プリセット一覧取得 `GET /character-presets`

- メソッド: `GET`
- パス: `/character-presets`

```json
{
  "presets": [
    { "id": 1, "name": "default", "memory_id": "default" }
  ],
  "active_id": 1
}
```

### 8.2 プリセット作成 `POST /character-presets`

```json
{
  "name": "work-mode",
  "system_prompt": "仕事モードのキャラクター設定...",
  "memory_id": "work"
}
```

### 8.3 プリセット取得 `GET /character-presets/{id}`

### 8.4 プリセット更新 `PATCH /character-presets/{id}`

```json
{
  "system_prompt": "更新後のプロンプト",
  "memory_id": "default"
}
```

### 8.5 プリセット削除 `DELETE /character-presets/{id}`

アクティブなプリセットは削除不可。

### 8.6 プリセット切り替え `POST /character-presets/{id}/activate`

```json
{
  "message": "Activated character preset 'work-mode'. Please restart the application.",
  "restart_required": true
}
```
