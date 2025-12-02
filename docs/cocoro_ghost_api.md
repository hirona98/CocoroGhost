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

- `user_id`: 将来的な拡張用。初期は `"default"` 固定想定。
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
