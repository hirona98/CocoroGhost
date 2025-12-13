# 設定DB（settings.db）仕様

## 目的

- アプリ起動に必要な最小設定（token等）と、運用中に切替えるプリセット（LLM/Character）を永続化する
- 記憶DB（`memory_<memory_id>.db`）とは分離する

## テーブル（必須）

### `global_settings`

- 単一行（グローバル）
- 固定トークンは **DBを正** とする（初回のみTOMLから投入してよい）

例カラム:

- `token`（TEXT）
- `exclude_keywords`（TEXT: JSON array）
- `reminders_enabled`（INTEGER: 0/1）
- `active_llm_preset_id`（INTEGER）
- `active_character_preset_id`（INTEGER）

### `llm_presets`

LLM/Embeddingの切替単位。LiteLLMの接続情報もここに持つ。

例カラム:

- `llm_model`, `llm_api_key`, `llm_base_url`, ...
- `embedding_model`, `embedding_dimension`, `embedding_base_url`, ...
- `max_inject_tokens`
- `similar_limit_by_kind_json`（種別ごとのKNN上限などをJSONで保持）

### `character_presets`

キャラクター（system prompt）と、紐づく `memory_id` を保持する。

例カラム:

- `system_prompt`
- `memory_id`

### `reminders`（任意 / 既存互換）

リマインダー（時刻＋内容）を保持する。

- `scheduled_at`（DATETIME）
- `content`（TEXT）

## 初回起動時（推奨フロー）

1. TOML（`config/setting.toml`）から `token` 等の最小値を読む
2. `settings.db` が空なら `global_settings/llm_presets/character_presets` の default を作る
3. 以降は `settings.db` を正として読み込む（TOMLは最小限の起動設定のみにする）
