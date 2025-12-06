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

## 5. 設定管理 API

### 5.1 全設定値取得 `GET,POST /settings`

共通設定と、現在アクティブなプリセットの詳細をまとめて取得する。

- メソッド: `GET`, `POST`
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
    "embedding_preset_name": "default",
    "embedding_model_api_key": "sk-...-embedding",
    "embedding_model": "text-embedding-3-small",
    "embedding_base_url": null,
    "embedding_dimension": 1536,
    "similar_episodes_limit": 5,
  }]
}
```
