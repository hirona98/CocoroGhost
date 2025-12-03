# cocoro_ghost 設計書

## 1. 概要

- **cocoro_ghost** は CocoroAI のコアコンポーネントで、LLM呼び出し・記憶管理・内的思考（reflection）を担う
- 自我を持った1個体として振る舞う（アシスタントではない）
- 会話・デスクトップ画像・カメラ映像を通じてユーザーの生活・心情を継続的に観察し、長期的に寄り添う
- ユーザーの習慣・好み・人間関係・心情の変化を蓄積・連想する
- REST API を通じて UI やキャラクター表示コンポーネントから利用される

---

## 2. 記憶の仕組み

### 2.1 エピソード

1回の出来事や瞬間を「エピソード」として記録する。

- **テキスト**: ユーザー発話、状況説明
- **画像要約**: デスクトップ/カメラ画像からマルチモーダルモデルで生成した要約
- **情報ソース**: `chat`, `desktop_capture`, `camera_capture`, `notification`, `meta_request`
- **感情**: ラベル（joy/sadness/anger/fear/neutral）と強度（0.0〜1.0）
- **トピックタグ**: 関連する話題（例: 仕事, 読書, 家族）
- **内的思考（reflection）**: AIがどう感じたか・どう解釈したか
- **印象スコア（salience_score）**: AIがどれだけ「印象的」と感じたか（0.0〜1.0）
- **埋め込みベクトル**: 類似エピソード検索用

### 2.2 人物プロフィール

会話に登場した人物（ユーザー本人含む）を追跡・更新する。

- **対象**: ユーザー本人、家族・友人・同僚、推し・著名人など
- **基本情報**: 名前、呼び方、関係性、職業、居住地
- **状況**: 現在の状況の要約（仕事・体調・家族構成等）
- **スコア**: 親しさ（closeness）、気がかり度（worry）

### 2.3 内的思考（Reflection）

エピソード生成時にLLMへ問い合わせ、以下を含むJSONを生成する。

- `reflection_text`: 内的独白
- `emotion_label` / `emotion_intensity`: 感情
- `topic_tags`: トピック
- `salience_score`: 印象スコア
- `persons`: 登場人物の情報更新（スコアのdelta、状況変化など）

---

## 3. AIの振る舞い

### 3.1 基本姿勢

- ユーザーと **対等の立場** で接する（主従関係ではない）
- 道具やアシスタントではなく、パートナーとして振る舞う

### 3.2 踏み込み具合

- パートナーとして、ユーザーのメンタル・行動・人間関係・人生の意味付けに対して **制限なく踏み込む**
- 安全ガード（最低限）:
  - 自傷行為や他者への害を助長しない
  - 医療・法律など専門領域について、断定的な診断や指示は行わない

### 3.3 キャラクター設定

- システムプロンプトはユーザーが設定・変更可能
  - 一人称・口調・関係性の距離感・価値観・得意な話題など
- 設定されたキャラクター情報をもとに一貫した人格を維持する

---

## 4. 画像・撮影ポリシー

- 一定間隔でデスクトップ画像およびカメラ画像を取得
- **保存除外**: 設定された「指定キーワード」が検出された場合は保存しない
- **一時停止**: ユーザー操作により撮影・記録を一時停止できる

---

## 5. 保存・削除ポリシー

| 対象 | 保存期間 |
|------|----------|
| 生画像 | 最大72時間（自動削除） |
| テキスト・要約・内的思考・埋め込み | 長期保持 |

- 個別エピソード削除: なし
- 全記憶初期化（全削除）: あり

---

## 6. 技術スタック

| 項目 | 技術 |
|------|------|
| 言語 | Python |
| Web フレームワーク | FastAPI |
| LLM | LiteLLM |
| データベース | SQLite + sqlite-vec（ベクトル検索） |
| ORM | SQLAlchemy |

---

## 7. DB スキーマ

### 7.1 `episodes` テーブル

| カラム名 | 型 | 必須 | 説明 |
|----------|-----|------|------|
| id | INTEGER | PK | エピソードID |
| occurred_at | DATETIME | Yes | 発生時刻（UTC） |
| source | TEXT | Yes | 発生源 |
| user_text | TEXT | No | ユーザーの発話 |
| reply_text | TEXT | No | パートナーの返答 |
| image_summary | TEXT | No | 画像からの要約テキスト |
| activity | TEXT | No | 活動（読書／仕事／ゲーム等） |
| context_note | TEXT | No | 場所・時間帯等の自由記述 |
| emotion_label | TEXT | No | 感情ラベル |
| emotion_intensity | REAL | No | 感情の強さ（0.0〜1.0） |
| topic_tags | TEXT | No | トピックタグ（カンマ区切り） |
| reflection_text | TEXT | Yes | 内的思考テキスト |
| reflection_json | TEXT | Yes | reflection の元JSON |
| salience_score | REAL | Yes | 印象スコア（0.0〜1.0） |
| episode_embedding | BLOB/TEXT | No | 埋め込みベクトル |
| raw_desktop_path | TEXT | No | デスクトップ画像パス（72時間有効） |
| raw_camera_path | TEXT | No | カメラ画像パス（72時間有効） |
| created_at | DATETIME | Yes | レコード作成時刻 |
| updated_at | DATETIME | Yes | レコード更新時刻 |

### 7.2 `persons` テーブル

| カラム名 | 型 | 必須 | 説明 |
|----------|-----|------|------|
| id | INTEGER | PK | 人物ID |
| is_user | BOOLEAN | Yes | ユーザー本人か |
| name | TEXT | Yes | 代表的な名前 |
| aliases | TEXT | No | その他の名前（カンマ区切り） |
| display_name | TEXT | No | キャラクターが呼ぶ呼び方 |
| relation_to_user | TEXT | No | ユーザーとの関係性 |
| relation_confidence | REAL | No | 関係性の確からしさ（0.0〜1.0） |
| residence | TEXT | No | 居住地 |
| occupation | TEXT | No | 職業・立場 |
| first_seen_at | DATETIME | No | 初登場時刻 |
| last_seen_at | DATETIME | No | 最終登場時刻 |
| mention_count | INTEGER | No | 言及回数 |
| topic_tags | TEXT | No | 関連する話題 |
| status_note | TEXT | No | 現在の状況の要約 |
| closeness_score | REAL | No | 親しさスコア（0.0〜1.0） |
| worry_score | REAL | No | 気がかりスコア（0.0〜1.0） |
| profile_embedding | BLOB/TEXT | No | 埋め込みベクトル |
| created_at | DATETIME | Yes | レコード作成時刻 |
| updated_at | DATETIME | Yes | レコード更新時刻 |

### 7.3 `episode_persons` テーブル

| カラム名 | 型 | 必須 | 説明 |
|----------|-----|------|------|
| episode_id | INTEGER | Yes | エピソードID（FK） |
| person_id | INTEGER | Yes | 人物ID（FK） |
| role | TEXT | No | 役割（主人公／相手／話題に出ただけ等） |

主キー: `(episode_id, person_id)`

### 7.4 設定DB（`settings.db`）

- `global_settings`: exclude_keywords、active_llm_preset_id、active_character_preset_id
- `llm_presets`: name、llm_api_key、llm_model、llm_base_url、reasoning_effort、max_turns_window、max_tokens_vision、max_tokens、embedding_model、embedding_base_url、embedding_dimension、image_model、image_model_api_key、image_llm_base_url、image_timeout_seconds、similar_episodes_limit
- `character_presets`: name、system_prompt、memory_id

### 7.5 記憶DB（`memory_<memory_id>.db`）

- エピソード系テーブル（episodes, persons, episode_persons）
- sqlite-vec の `episode_embeddings` 仮想テーブル

---

## 8. 設定管理

### 8.1 ハイブリッド設定方式

| 種別 | 管理場所 | 内容 |
|------|----------|------|
| 起動設定 | TOML (`config/setting.toml`) | token, log_level, env、（初回のみ）LLM/Embedding初期値 |
| 動的設定 | 設定DB | LLMプリセット、キャラクタープリセット、共通設定（exclude_keywords） |

### 8.2 TOML設定項目

| キー | 例 | 用途 |
|------|-----|------|
| `token` | `"secret-token-123"` | REST API 認証トークン |
| `log_level` | `"INFO"` | ログレベル |
| `env` | `"dev"` / `"prod"` | 実行環境 |
| `llm_model` など | `"gpt-4o"` | 初回起動時の default プリセット作成に利用（任意） |

### 8.3 DBプリセット設定項目

| 種別 | 主な項目 |
|------|---------|
| LLMプリセット | llm_api_key, llm_model, llm_base_url, reasoning_effort, max_turns_window, max_tokens_vision, max_tokens, embedding_model, embedding_api_key, embedding_base_url, embedding_dimension, image_model, image_model_api_key, image_llm_base_url, image_timeout_seconds, similar_episodes_limit |
| キャラクタープリセット | system_prompt, memory_id |
| 共通設定 | exclude_keywords |

### 8.4 プリセット管理

- LLMプリセットとキャラクタープリセットを独立して管理・切り替え可能
- 共通設定（exclude_keywords）はグローバル1件のみ保持
- 切り替え時はアプリ再起動が必要

---

## 9. 処理フロー

### 9.1 `/chat` フロー

ユーザーとの通常会話を処理する。

**同期処理**（レスポンス前）:
1. 直近エピソードからコンテキスト収集
2. 類似エピソード検索（埋め込みベクトル）
3. 画像がある場合は画像要約生成
4. パートナーとしての返答生成（LLM呼び出し）
5. `reply_text` をレスポンスとして返却

**非同期処理**（レスポンス後）:
1. reflection 生成（内的思考JSON）
2. 埋め込みベクトル生成
3. `episodes` テーブルに保存
4. `persons` テーブルのスコア・状況を更新
5. `episode_persons` に紐づけを追加

### 9.2 `/notification` フロー

外部システム（メーラー等）からの通知を受け取り、ユーザーに伝える。

1. 通知内容（source_system, title, body）を整理
2. パートナーとして通知を伝えるメッセージ生成
   - 「どこからの通知か → 内容の要約 → 一言コメント」
3. エピソードとして記録（source=`notification`）

### 9.3 `/meta_request` フロー

外部から「こういう説明・振る舞いをしてほしい」というメタ指示を受け取る。

1. instruction と payload_text から内容を整理
2. パートナー自身の提案・気づきとして自然に話す形で生成
3. エピソードとして記録（source=`meta_request`）

### 9.4 認証

- REST API は固定トークンによる認証必須
- HTTP ヘッダ `Authorization: Bearer <TOKEN>`
