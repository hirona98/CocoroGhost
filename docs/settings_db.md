# 設定DB（settings.db）仕様

## 目的

- アプリ起動に必要な最小設定（token / log_level）と、運用中に切替えるプリセット（LLM/Embedding）を永続化する
- 記憶DB（`memory_<memory_id>.db`）とは分離する

## 使い方（運用）

- 起動時に `settings.db` が無ければ自動作成され、`global_settings` と各種 `*_presets` の初期値が投入される（`docs/bootstrap.md` も参照）。
- UIは原則 `/api/settings`（GET/PUT）を通して読み書きする。
- `active_*_preset_id` を切り替えることで、LLM/Embedding/Persona/Addon（任意追加オプション）を運用中に切替できる。
- **EmbeddingPreset.id はそのまま `memory_id` として使う**ため、`active_embedding_preset_id` の切替は「参照する記憶DBファイルが変わる」ことを意味する。
- 内蔵Worker運用のため、`/api/settings` 更新後は内蔵Workerが自動再起動して設定変更に追従する（LLM接続・memory_id など）。

## テーブル

### `global_settings`

- 単一行（グローバル）
- token は **DBを正** とする（初回のみTOMLから投入してよい）

#### カラム（実装準拠）

- `token`（TEXT）
- `exclude_keywords`（TEXT: JSON array）
- `memory_enabled`（INTEGER: 0/1）
- `reminders_enabled`（INTEGER: 0/1）
- `active_llm_preset_id`（TEXT: UUID）
- `active_embedding_preset_id`（TEXT: UUID / `memory_id`）
- `active_persona_preset_id`（TEXT: UUID）
- `active_contract_preset_id`（TEXT: UUID）: addon用（アプリ/コード上の呼称は `active_addon_preset_id`）
- `created_at`（DATETIME）
- `updated_at`（DATETIME）

補足:
- `exclude_keywords` は `/api/chat` の入力を弾くための簡易フィルタ（パターン文字を含む場合は正規表現として扱われる）。
- `active_*_preset_id` は「アーカイブされていないプリセット」に対してのみ有効。

### `llm_presets`

LLMの切替単位。LiteLLMの接続情報（chat/image）をここに持つ。

#### カラム（実装準拠）

- `id`（TEXT: UUID）
- `name`（TEXT: unique）
- `archived`（INTEGER: 0/1）
- `llm_api_key`（TEXT）
- `llm_model`（TEXT）
- `llm_base_url`（TEXT, nullable）
- `reasoning_effort`（TEXT, nullable）
- `max_turns_window`（INTEGER）: LLMへ渡す直近会話数（conversation window）
- `max_tokens`（INTEGER）: chatのmax_tokens
- `image_model`（TEXT）
- `image_model_api_key`（TEXT, nullable）
- `image_llm_base_url`（TEXT, nullable）
- `max_tokens_vision`（INTEGER）
- `image_timeout_seconds`（INTEGER）
- `created_at` / `updated_at`（DATETIME）

補足:
- `llm_api_key` と `image_model_api_key` は別にできる（未指定時は同一キー運用）。
- base_url系は LiteLLM の接続先切替用。

### `embedding_presets`

Embedding/検索パラメータの切替単位。

#### カラム（実装準拠）

- `id`（TEXT: UUID）: **memory_id としても使う**
- `name`（TEXT: 表示名）
- `archived`（INTEGER: 0/1）
- `embedding_model`（TEXT）
- `embedding_api_key`（TEXT, nullable）
- `embedding_base_url`（TEXT, nullable）
- `embedding_dimension`（INTEGER）: `memory_<id>.db` の vec0 次元と一致必須
- `similar_episodes_limit`（INTEGER）: Retrieverが返す relevant episodes の上限
- `max_inject_tokens`（INTEGER）: Schedulerが組む MemoryPack の注入予算（近似で char budget に変換）
- `similar_limit_by_kind_json`（TEXT: JSON object）: 将来用（kind別の上限など）
- `created_at` / `updated_at`（DATETIME）

補足:

- 記憶DBは `memory_<embedding_preset_id>.db`（`embedding_preset_id` はUUID）として管理する
- `embedding_dimension` を変える場合は **別id（別DB）** を作るのが安全（同じDBで次元変更すると初期化でエラーになる）。

### `persona_presets`

persona（人格コア）プロンプトの切替単位。

#### カラム（実装準拠）

- `id`（TEXT: UUID）
- `name`（TEXT）
- `archived`（INTEGER: 0/1）
- `persona_text`（TEXT）
- `created_at` / `updated_at`（DATETIME）

### `contract_presets`

addon（personaへの任意追加オプション）プロンプトの切替単位（テーブル名は `contract_presets`）。

#### カラム（実装準拠）

- `id`（TEXT: UUID）
- `name`（TEXT）
- `archived`（INTEGER: 0/1）
- `contract_text`（TEXT）: addon本文（アプリ側の呼称は `addon_text`）
- `created_at` / `updated_at`（DATETIME）

### `reminders`（任意）

リマインダー（時刻＋内容）を保持する。

#### カラム（実装準拠）

- `id`（TEXT: UUID）
- `enabled`（INTEGER: 0/1）
- `scheduled_at`（DATETIME）
- `content`（TEXT）
- `created_at` / `updated_at`（DATETIME）

補足:
- 現状の `/api/settings` 更新は reminders を「全置換」で作り直すため、`enabled` は常にデフォルト（true）になりやすい。

## 初期化（起動時）

1. TOML（`config/setting.toml`）から `token` / `log_level` を読む
2. `settings.db` が空なら `global_settings` と各種 `*_presets` の default を作り、`active_*_preset_id` を設定する
