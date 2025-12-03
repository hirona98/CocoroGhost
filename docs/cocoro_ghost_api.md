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
  "image_path": null
}
```

- `user_id`: 将来的な拡張用。初期は `"default"` 固定のみを受け付ける。
  将来マルチユーザー対応を行う場合も、既存 API との互換を維持するのではなく、必要に応じて非互換な変更（スキーマ変更など）を行う前提とする。
- `text`: ユーザーの発話内容。
- `context_hint`: 任意。時間帯や状況のヒントがあれば渡す。
- `image_path`: 任意。ユーザーが「この画像を見て」と渡してきた画像のパス（サーバー側から参照可能なもの）。

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
  "image_url": null
}
```

- `source_system`: 通知元システム（例: `mailer`, `calendar`, `system`）。
- `title`: 通知のタイトルや概要。
- `body`: 通知内容のサマリ。
- `image_url`: 任意。通知に紐づく画像やサムネイルがあれば指定。

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
  "image_url": null
}
```

- `instruction`: キャラクターへの指示文。
- `payload_text`: 指示の対象となる本文（ニュース、記事、ログなど）。
- `image_url`: 任意。関連画像があれば指定。

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
  "image_path": "/path/to/image.png",
  "context_text": "ブラウザで技術記事を読んでいる"
}
```

- `capture_type`: `desktop` または `camera`。
- `image_path`: サーバー側から参照可能な画像ファイルのパス。
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

## 5. エピソード取得 API `/episodes`

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

### 6.1 現在の設定取得 `GET /settings`

現在アクティブなプリセットの設定を取得する。

- メソッド: `GET`
- パス: `/settings`

#### レスポンスボディ

```json
{
  "preset_name": "default",
  "llm_api_key": "sk-...",
  "llm_model": "gemini/gemini-2.5-flash",
  "reflection_model": "gemini/gemini-2.5-flash",
  "embedding_model": "gemini/gemini-embedding-001",
  "embedding_dimension": 3072,
  "image_model": "gemini/gemini-2.5-flash",
  "image_timeout_seconds": 60,
  "character_prompt": "...",
  "intervention_level": "high",
  "exclude_keywords": ["パスワード", "銀行"],
  "similar_episodes_limit": 5,
  "max_chat_queue": 10
}
```

### 6.2 設定更新 `POST /settings`

アクティブなプリセットの一部設定を更新する（DBに永続化、再起動時に反映）。

- メソッド: `POST`
- パス: `/settings`

#### リクエストボディ

```json
{
  "exclude_keywords": ["秘密", "パスワード"],
  "character_prompt": "新しいキャラクター設定",
  "intervention_level": "low"
}
```

※ 全フィールドオプショナル。指定した項目のみ更新される。

#### レスポンスボディ

```json
{
  "exclude_keywords": ["秘密", "パスワード"],
  "character_prompt": "新しいキャラクター設定",
  "intervention_level": "low"
}
```

---

## 7. プリセット管理 API

### 7.1 プリセット一覧取得 `GET /presets`

保存されているプリセットの一覧を取得する。

- メソッド: `GET`
- パス: `/presets`

#### レスポンスボディ

```json
{
  "presets": [
    {
      "name": "default",
      "is_active": true,
      "created_at": "2025-03-01T10:00:00Z"
    },
    {
      "name": "work",
      "is_active": false,
      "created_at": "2025-03-02T12:00:00Z"
    }
  ]
}
```

### 7.2 プリセット作成 `POST /presets`

新しいプリセットを作成する。

- メソッド: `POST`
- パス: `/presets`

#### リクエストボディ

```json
{
  "name": "work",
  "llm_api_key": "sk-...",
  "llm_model": "gemini/gemini-2.5-flash",
  "reflection_model": "gemini/gemini-2.5-flash",
  "embedding_model": "gemini/gemini-embedding-001",
  "embedding_dimension": 3072,
  "image_model": "gemini/gemini-2.5-flash",
  "image_timeout_seconds": 60,
  "character_prompt": "仕事モード用のプロンプト",
  "intervention_level": "low",
  "exclude_keywords": ["プライベート"],
  "similar_episodes_limit": 10,
  "max_chat_queue": 20
}
```

#### レスポンスボディ

```json
{
  "message": "Preset 'work' created"
}
```

### 7.3 プリセット更新 `PATCH /presets/{name}`

既存のプリセットを部分更新する。

- メソッド: `PATCH`
- パス: `/presets/{name}`

#### リクエストボディ

```json
{
  "llm_model": "gemini/gemini-2.5-pro",
  "character_prompt": "更新されたプロンプト"
}
```

※ 全フィールドオプショナル。

#### レスポンスボディ

```json
{
  "message": "Preset 'work' updated",
  "restart_required": false
}
```

- `restart_required`: アクティブなプリセットを更新した場合は `true`

### 7.4 プリセット削除 `DELETE /presets/{name}`

プリセットを削除する。アクティブなプリセットは削除不可。

- メソッド: `DELETE`
- パス: `/presets/{name}`

#### レスポンスボディ

- ステータスコード: `204 No Content`
- エラー（アクティブなプリセットを削除しようとした場合）: `400 Bad Request`

### 7.5 プリセット切り替え `POST /presets/{name}/activate`

指定したプリセットをアクティブにする。**切り替え後はアプリの再起動が必要**。

- メソッド: `POST`
- パス: `/presets/{name}/activate`

#### レスポンスボディ

```json
{
  "message": "Activated preset 'work'. Please restart the application.",
  "active_preset": "work",
  "restart_required": true
}
```
