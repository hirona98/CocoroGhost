# 設定DB（settings.db）仕様

## 目的

- アプリ起動に必要な最小設定（token / log_level）と、運用中に切替えるプリセット（LLM/Embedding）を永続化する
- 記憶DB（`memory_<memory_id>.db`）とは分離する

## テーブル

### `global_settings`

- 単一行（グローバル）
- token は **DBを正** とする（初回のみTOMLから投入してよい）

例カラム:

- `token`（TEXT）
- `exclude_keywords`（TEXT: JSON array）
- `reminders_enabled`（INTEGER: 0/1）
- `active_llm_preset_id`（TEXT: UUID）
- `active_embedding_preset_id`（TEXT: UUID / `memory_id`）
- `active_persona_preset_id`（TEXT: UUID）
- `active_contract_preset_id`（TEXT: UUID）

### `llm_presets`

LLMの切替単位。LiteLLMの接続情報（chat/image）をここに持つ。

例カラム:

- `archived`（INTEGER: 0/1）
- `llm_model`, `llm_api_key`, `llm_base_url`, ...
- `image_model`, `image_model_api_key`, `image_llm_base_url`, ...

### `embedding_presets`

Embedding/検索パラメータの切替単位。

例カラム:

- `name`（TEXT: 表示名）
- `archived`（INTEGER: 0/1）
- `embedding_model`, `embedding_dimension`, `embedding_base_url`, ...
- `max_inject_tokens`
- `similar_limit_by_kind_json`（種別ごとのKNN上限などをJSONで保持）
- `similar_episodes_limit`

補足:

- 記憶DBは `memory_<embedding_preset_id>.db`（`embedding_preset_id` はUUID）として管理する

### `persona_presets`

persona（人格コア）プロンプトの切替単位。

例カラム:

- `name`（TEXT）
- `archived`（INTEGER: 0/1）
- `persona_text`（TEXT）

### `contract_presets`

contract（関係契約）プロンプトの切替単位。

例カラム:

- `name`（TEXT）
- `archived`（INTEGER: 0/1）
- `contract_text`（TEXT）

### `reminders`（任意）

リマインダー（時刻＋内容）を保持する。

- `scheduled_at`（DATETIME）
- `content`（TEXT）

## 初期化（起動時）

1. TOML（`config/setting.toml`）から `token` / `log_level` を読む
2. `settings.db` が空なら `global_settings` と各種 `*_presets` の default を作り、`active_*_preset_id` を設定する
3. 以降は `settings.db` を正として読み込む（TOMLは最小限の起動設定のみにする）
