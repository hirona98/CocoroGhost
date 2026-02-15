# 階層型世界モデル World Model

## 1. 目的

本書は、階層型世界モデルにおける World Model 永続化仕様を定義する。

## 2. スコープ

対象:

1. Entity/Relation/Belief/Intent の永続化モデル
2. 主キー/一意制約/必須インデックス
3. 競合時の並存規約
4. `world_model_update_request` の適用規約

非対象:

1. Capability Registry 詳細（`03_capability_registry.md`）
2. Event/WritePlan 連携詳細（`04_event_writeplan連携.md`）
3. capability 個別仕様（`05_web_access.md`）

## 3. 既存基盤との境界

### 3.1 保存責務

| 対象 | 正の保存先 | 役割 |
|---|---|---|
| 生ログ（会話/通知/視覚/自律結果） | `events` | 事実ログ（追記中心） |
| 会話向け育つ記憶 | `state` | 人が読める記憶本文 |
| 自律判断向け構造化状態 | `wm_*` | 構造化知識と意図状態 |
| 改訂履歴 | `revisions` | 更新理由と根拠追跡 |

### 3.2 境界ルール

1. `events` は観測事実ログとして維持し、Observe 入力に使う
2. `state` は会話品質向上用途で維持し、`wm_*` の代替にしない
3. 自律判断の正は `wm_*` とする
4. `wm_*` 更新時は `revisions` に理由と根拠を残す
5. `/api/chat` 同期経路で重い world model 更新を行わない

## 4. 論理モデル

1. Entity
   - 人/場所/物/デバイス/Webリソース/自己状態
2. Relation
   - entity 間関係
3. Belief
   - 命題 + 確信度 + 有効期間 + 根拠
4. Intent State
   - goal/ticket/result
5. Observation（補助）
   - event/action_result/system trigger の正規化入力
6. Link（補助）
   - belief/goal/ticket/result/observation 間の因果リンク

## 5. 最小テーブル仕様（`memory_<embedding_preset_id>.db`）

### 5.1 `wm_entities`

主要カラム:

1. `entity_id` INTEGER PK
2. `entity_key` TEXT UNIQUE NOT NULL
3. `entity_type` TEXT NOT NULL
4. `display_name` TEXT NOT NULL
5. `canonical_name` TEXT NOT NULL
6. `attributes_json` TEXT NOT NULL（default `{}`）
7. `salience` REAL NOT NULL
8. `first_seen_at` INTEGER NOT NULL
9. `last_seen_at` INTEGER NOT NULL
10. `created_at` INTEGER NOT NULL
11. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_entities_type` (`entity_type`)
2. `idx_wm_entities_last_seen` (`last_seen_at`)

### 5.2 `wm_relations`

主要カラム:

1. `relation_id` INTEGER PK
2. `relation_key` TEXT UNIQUE NOT NULL
3. `from_entity_id` INTEGER NOT NULL
4. `to_entity_id` INTEGER NOT NULL
5. `relation_type` TEXT NOT NULL
6. `qualifier_json` TEXT NOT NULL（default `{}`）
7. `confidence` REAL NOT NULL
8. `status` TEXT NOT NULL（`active|inactive`）
9. `valid_from_ts` INTEGER NULL
10. `valid_to_ts` INTEGER NULL
11. `last_observed_at` INTEGER NOT NULL
12. `created_at` INTEGER NOT NULL
13. `updated_at` INTEGER NOT NULL

制約:

1. UNIQUE（`relation_key`）

必須インデックス:

1. `idx_wm_relations_from` (`from_entity_id`)
2. `idx_wm_relations_to` (`to_entity_id`)
3. `idx_wm_relations_type` (`relation_type`)

### 5.3 `wm_beliefs`

主要カラム:

1. `belief_id` INTEGER PK
2. `belief_key` TEXT UNIQUE NOT NULL
3. `subject_entity_id` INTEGER NULL
4. `predicate` TEXT NOT NULL
5. `object_entity_id` INTEGER NULL
6. `object_text` TEXT NULL
7. `value_json` TEXT NOT NULL（default `{}`）
8. `confidence` REAL NOT NULL
9. `status` TEXT NOT NULL（`active|superseded|retracted`）
10. `valid_from_ts` INTEGER NULL
11. `valid_to_ts` INTEGER NULL
12. `first_observed_at` INTEGER NOT NULL
13. `last_observed_at` INTEGER NOT NULL
14. `evidence_event_ids_json` TEXT NOT NULL（default `[]`）
15. `evidence_result_ids_json` TEXT NOT NULL（default `[]`）
16. `created_at` INTEGER NOT NULL
17. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_beliefs_subject_predicate` (`subject_entity_id`, `predicate`)
2. `idx_wm_beliefs_object` (`object_entity_id`)
3. `idx_wm_beliefs_status` (`status`)

### 5.4 `wm_strategic_goals`

主要カラム:

1. `goal_id` TEXT PK（UUID）
2. `goal_type` TEXT NOT NULL
3. `title` TEXT NOT NULL
4. `intent` TEXT NOT NULL
5. `priority` REAL NOT NULL
6. `horizon_seconds` INTEGER NOT NULL
7. `success_criteria_json` TEXT NOT NULL（default `[]`）
8. `constraints_json` TEXT NOT NULL（default `[]`）
9. `status` TEXT NOT NULL（`active|paused|done|dropped`）
10. `reason_code` TEXT NULL
11. `created_at` INTEGER NOT NULL
12. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_goals_status_priority` (`status`, `priority`)
2. `idx_wm_goals_updated_at` (`updated_at`)

### 5.5 `wm_action_tickets`

主要カラム:

1. `ticket_id` TEXT PK（UUID）
2. `goal_id` TEXT NOT NULL
3. `capability_id` TEXT NOT NULL
4. `operation` TEXT NOT NULL
5. `input_payload_json` TEXT NOT NULL（default `{}`）
6. `preconditions_json` TEXT NOT NULL（default `[]`）
7. `expected_effect_json` TEXT NOT NULL（default `[]`）
8. `verify_json` TEXT NOT NULL（default `[]`）
9. `status` TEXT NOT NULL（`queued|running|succeeded|failed|cancelled|expired`）
10. `reason_code` TEXT NULL
11. `issued_at` INTEGER NOT NULL
12. `deadline_at` INTEGER NULL
13. `created_at` INTEGER NOT NULL
14. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_tickets_goal_status` (`goal_id`, `status`)
2. `idx_wm_tickets_status_deadline` (`status`, `deadline_at`)

### 5.6 `wm_action_results`

主要カラム:

1. `result_id` TEXT PK（UUID）
2. `ticket_id` TEXT NOT NULL
3. `status` TEXT NOT NULL（`succeeded|failed`）
4. `observations_json` TEXT NOT NULL（default `[]`）
5. `effects_json` TEXT NOT NULL（default `[]`）
6. `error_message` TEXT NULL
7. `reason_code` TEXT NULL
8. `finished_at` INTEGER NOT NULL
9. `created_at` INTEGER NOT NULL
10. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_results_ticket` (`ticket_id`)
2. `idx_wm_results_finished_at` (`finished_at`)

### 5.7 `wm_observations`

主要カラム:

1. `observation_id` TEXT PK（UUID）
2. `source_type` TEXT NOT NULL（`event|action_result|system`）
3. `source_ref` TEXT NULL（例: `event:123`）
4. `summary` TEXT NOT NULL
5. `payload_json` TEXT NOT NULL（default `{}`）
6. `importance` REAL NOT NULL
7. `observed_at` INTEGER NOT NULL
8. `created_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_observations_source` (`source_type`, `source_ref`)
2. `idx_wm_observations_observed_at` (`observed_at`)

### 5.8 `wm_links`

主要カラム:

1. `link_id` INTEGER PK
2. `link_type` TEXT NOT NULL（`caused_by|supports|contradicts|evidence_for`）
3. `from_kind` TEXT NOT NULL（`belief|goal|ticket|result|observation|event`）
4. `from_id` TEXT NOT NULL
5. `to_kind` TEXT NOT NULL（`belief|goal|ticket|result|observation|event`）
6. `to_id` TEXT NOT NULL
7. `confidence` REAL NOT NULL
8. `evidence_event_ids_json` TEXT NOT NULL（default `[]`）
9. `created_at` INTEGER NOT NULL

制約:

1. UNIQUE（`link_type`, `from_kind`, `from_id`, `to_kind`, `to_id`）

必須インデックス:

1. `idx_wm_links_from` (`from_kind`, `from_id`)
2. `idx_wm_links_to` (`to_kind`, `to_id`)

## 6. 更新I/F

### 6.1 `world_model_update_request`

```json
{
  "request_id": "uuid",
  "trigger_type": "event_created|action_result|periodic|startup",
  "observation_ids": ["uuid"],
  "operations": [
    {"op": "upsert_entity", "payload": {}},
    {"op": "upsert_relation", "payload": {}},
    {"op": "upsert_belief", "payload": {}},
    {"op": "upsert_goal", "payload": {}},
    {"op": "upsert_ticket", "payload": {}},
    {"op": "insert_result", "payload": {}},
    {"op": "insert_link", "payload": {}}
  ],
  "requested_at": "2026-02-15T12:00:00"
}
```

### 6.2 適用順序（1 request = 1 transaction）

1. entity upsert
2. relation upsert
3. belief upsert
4. goal/ticket/result upsert
5. link insert
6. revisions 追記

ルール:

1. 途中失敗時は transaction 全体 rollback
2. fail-fast を正とする
3. fallback を導入しない

## 7. 競合規約

### 7.1 Entity

1. `entity_key` 一致は同一 entity として upsert
2. `display_name` は最新観測で更新可
3. `canonical_name` は正規化結果を維持

### 7.2 Relation

1. `relation_key` 一致は同一 relation として upsert
2. `confidence` は `max(old, incoming)` を採用
3. 時間矛盾は上書きせず `valid_*` で並存管理

### 7.3 Belief

1. `belief_key` 一致は同一命題として upsert
2. 衝突命題は別行で並存
3. `status=retracted` は Reflect の明示判断のみで更新

### 7.4 Intent State

1. `goal_id` / `ticket_id` / `result_id` は再利用しない
2. 状態遷移は `01_基盤ループ.md` を正とする
3. 状態更新時は `reason_code` を必須保存

## 8. revisions 連携規約

1. `wm_*` 更新時は `revisions.entity_type` に対象種別を記録
2. `before_json` / `after_json` を必須保存
3. `reason` は reason_code と整合した短文を保存
4. 根拠 event がある更新は `evidence_event_ids_json` を空にしない
5. `revisions.entity_id` は `TEXT` で統一し、整数主キーは10進文字列で保存する
