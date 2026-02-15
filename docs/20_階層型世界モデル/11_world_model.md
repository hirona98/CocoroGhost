# 階層型世界モデル World Model

## 1. 目的

本書は、階層型世界モデルにおける World Model の永続化仕様を定義する。
Phase 2 の目的は次の2点。

1. 最小テーブル/モデル案を固定する
2. 既存 `events` / `state` / `revisions` との境界を固定する

## 2. スコープ

対象:

1. World Model の論理層（Entity/Relation/Belief/Intent）
2. 最小テーブル定義（主キー/一意制約/必須インデックス）
3. 更新規約（upsert、競合、並存）
4. 基盤ループ（Observe/Reflect）からの反映I/F

非対象:

1. Capability Registry の詳細（Phase 3）
2. Event/WritePlan 連携詳細（Phase 4）
3. `web_access` 能力の個別仕様（Phase 5）
4. 実コード実装（Phase 6）

## 3. 既存基盤との境界

## 3.1 保存責務の分担

| 対象 | 正の保存先 | 役割 |
|---|---|---|
| 生ログ（会話/通知/視覚/リマインダー/自発発話） | `events` | 事実ログ。追記中心 |
| 会話向けの育つ記憶 | `state` | 人が読める記憶本文（fact/relation/summary等） |
| 自律判断向けの構造化世界状態 | `wm_*` | 構造化知識と意図状態 |
| 変更履歴 | `revisions` | 更新理由と根拠を追跡 |

## 3.2 境界ルール

1. `events` は観測の事実ログとして維持し、World Model の入力に使う
2. `state` は会話品質向上のための読み物として維持し、World Model の代替にしない
3. World Model の正は `wm_*` とし、戦略/戦術/実行/反省ループの判断は `wm_*` を参照する
4. World Model 更新時も `revisions` へ理由と根拠を残す
5. `/api/chat` 同期経路では World Model の重い更新を行わない

## 4. 論理モデル

World Model は 4 層で定義する。

1. Entity
   - 人/場所/物/デバイス/Webリソース/自己状態
2. Relation
   - entity 間の関係（`located_in` / `owns` / `connected_to` など）
3. Belief
   - 命題 + 確信度 + 有効期間
4. Intent State
   - `StrategicGoal` / `ActionTicket` / `ActionResult`

補助層:

1. Observation
   - `events` / `ActionResult` から正規化した入力
2. Link
   - belief/goal/action 間の因果・根拠リンク

## 5. 最小テーブル仕様（`memory_<embedding_preset_id>.db`）

## 5.1 `wm_entities`

目的:

- 構造化対象の実体を正規化して管理する

主要カラム:

1. `entity_id` INTEGER PK
2. `entity_key` TEXT UNIQUE NOT NULL
3. `entity_type` TEXT NOT NULL
4. `display_name` TEXT NOT NULL
5. `canonical_name` TEXT NOT NULL
6. `attributes_json` TEXT NOT NULL（default `"{}"`）
7. `first_seen_at` INTEGER NOT NULL
8. `last_seen_at` INTEGER NOT NULL
9. `created_at` INTEGER NOT NULL
10. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_entities_type` (`entity_type`)
2. `idx_wm_entities_last_seen` (`last_seen_at`)

## 5.2 `wm_relations`

目的:

- entity 間の関係を構造化して管理する

主要カラム:

1. `relation_id` INTEGER PK
2. `relation_key` TEXT UNIQUE NOT NULL
3. `from_entity_id` INTEGER NOT NULL（FK -> `wm_entities.entity_id`）
4. `to_entity_id` INTEGER NOT NULL（FK -> `wm_entities.entity_id`）
5. `relation_type` TEXT NOT NULL
6. `qualifier_json` TEXT NOT NULL（default `"{}"`）
7. `confidence` REAL NOT NULL
8. `status` TEXT NOT NULL（`active|inactive`）
9. `valid_from_ts` INTEGER NULL
10. `valid_to_ts` INTEGER NULL
11. `last_observed_at` INTEGER NOT NULL
12. `created_at` INTEGER NOT NULL
13. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_relations_from` (`from_entity_id`)
2. `idx_wm_relations_to` (`to_entity_id`)
3. `idx_wm_relations_type` (`relation_type`)

## 5.3 `wm_beliefs`

目的:

- 命題と確信度を管理し、矛盾を上書きせず並存させる

主要カラム:

1. `belief_id` INTEGER PK
2. `belief_key` TEXT UNIQUE NOT NULL
3. `subject_entity_id` INTEGER NULL（FK -> `wm_entities.entity_id`）
4. `predicate` TEXT NOT NULL
5. `object_entity_id` INTEGER NULL（FK -> `wm_entities.entity_id`）
6. `object_text` TEXT NULL
7. `value_json` TEXT NOT NULL（default `"{}"`）
8. `confidence` REAL NOT NULL
9. `status` TEXT NOT NULL（`active|superseded|retracted`）
10. `valid_from_ts` INTEGER NULL
11. `valid_to_ts` INTEGER NULL
12. `first_observed_at` INTEGER NOT NULL
13. `last_observed_at` INTEGER NOT NULL
14. `evidence_event_ids_json` TEXT NOT NULL（default `"[]"`）
15. `evidence_result_ids_json` TEXT NOT NULL（default `"[]"`）
16. `created_at` INTEGER NOT NULL
17. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_beliefs_subject_predicate` (`subject_entity_id`, `predicate`)
2. `idx_wm_beliefs_object` (`object_entity_id`)
3. `idx_wm_beliefs_status` (`status`)

## 5.4 `wm_strategic_goals`

目的:

- `StrategicGoal` の正を保持する

主要カラム:

1. `goal_id` TEXT PK（UUID）
2. `title` TEXT NOT NULL
3. `intent` TEXT NOT NULL
4. `priority` REAL NOT NULL
5. `horizon_seconds` INTEGER NOT NULL
6. `success_criteria_json` TEXT NOT NULL（default `"[]"`）
7. `constraints_json` TEXT NOT NULL（default `"[]"`）
8. `status` TEXT NOT NULL（`active|paused|done|dropped`）
9. `reason_code` TEXT NULL
10. `created_at` INTEGER NOT NULL
11. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_goals_status_priority` (`status`, `priority`)
2. `idx_wm_goals_updated_at` (`updated_at`)

## 5.5 `wm_action_tickets`

目的:

- `ActionTicket` の正を保持する

主要カラム:

1. `ticket_id` TEXT PK（UUID）
2. `goal_id` TEXT NOT NULL（FK -> `wm_strategic_goals.goal_id`）
3. `capability_id` TEXT NOT NULL
4. `operation` TEXT NOT NULL
5. `input_payload_json` TEXT NOT NULL（default `"{}"`）
6. `preconditions_json` TEXT NOT NULL（default `"[]"`）
7. `expected_effect_json` TEXT NOT NULL（default `"[]"`）
8. `verify_json` TEXT NOT NULL（default `"[]"`）
9. `status` TEXT NOT NULL（`queued|running|succeeded|failed|cancelled|expired`）
10. `reason_code` TEXT NULL
11. `issued_at` INTEGER NOT NULL
12. `deadline_at` INTEGER NULL
13. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_tickets_goal_status` (`goal_id`, `status`)
2. `idx_wm_tickets_status_deadline` (`status`, `deadline_at`)

## 5.6 `wm_action_results`

目的:

- `ActionResult` の正を保持する

主要カラム:

1. `result_id` INTEGER PK
2. `ticket_id` TEXT NOT NULL（FK -> `wm_action_tickets.ticket_id`）
3. `status` TEXT NOT NULL（`succeeded|failed`）
4. `observations_json` TEXT NOT NULL（default `"[]"`）
5. `effects_json` TEXT NOT NULL（default `"[]"`）
6. `error_message` TEXT NULL
7. `reason_code` TEXT NULL
8. `finished_at` INTEGER NOT NULL
9. `created_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_results_ticket` (`ticket_id`)
2. `idx_wm_results_finished_at` (`finished_at`)

## 5.7 `wm_observations`

目的:

- Observe ループの正規化結果を保持する

主要カラム:

1. `observation_id` TEXT PK（UUID）
2. `source_type` TEXT NOT NULL（`event|action_result`）
3. `source_ref` TEXT NOT NULL（例: `event:123`）
4. `summary` TEXT NOT NULL
5. `payload_json` TEXT NOT NULL（default `"{}"`）
6. `importance` REAL NOT NULL
7. `observed_at` INTEGER NOT NULL
8. `created_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_observations_source` (`source_type`, `source_ref`)
2. `idx_wm_observations_observed_at` (`observed_at`)

## 5.8 `wm_links`

目的:

- belief/goal/ticket/result の因果・根拠を横断リンクする

主要カラム:

1. `link_id` INTEGER PK
2. `link_type` TEXT NOT NULL（`caused_by|supports|contradicts|evidence_for`）
3. `from_kind` TEXT NOT NULL（`belief|goal|ticket|result|observation`）
4. `from_id` TEXT NOT NULL
5. `to_kind` TEXT NOT NULL（`belief|goal|ticket|result|observation|event`）
6. `to_id` TEXT NOT NULL
7. `confidence` REAL NOT NULL
8. `evidence_event_ids_json` TEXT NOT NULL（default `"[]"`）
9. `created_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_links_from` (`from_kind`, `from_id`)
2. `idx_wm_links_to` (`to_kind`, `to_id`)

## 6. 更新I/F（Observe/Reflect からの反映）

## 6.1 `world_model_update_request` 形式

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

## 6.2 適用順序（1 request = 1 transaction）

1. entity upsert
2. relation upsert
3. belief upsert
4. goal/ticket/result upsert
5. link insert
6. `revisions` 追記

ルール:

1. 途中失敗時は transaction 全体を rollback する
2. fail-fast を正とし、fallback を入れない
3. 実行失敗は worker の再試行ポリシーに委譲する

## 7. 競合と並存の規約

## 7.1 Entity

1. `entity_key` 一致は同一entityとして upsert
2. `display_name` は最新観測で更新してよい
3. `canonical_name` は正規化結果を維持する

## 7.2 Relation

1. `relation_key` 一致は同一relationとして upsert
2. `confidence` は `max(old, incoming)` を採用する
3. 時間矛盾は上書きせず `valid_*` を調整して並存させる

## 7.3 Belief

1. `belief_key` 一致は同一命題として upsert
2. 衝突命題（同subject+predicateで内容不一致）は上書きせず別行で並存させる
3. `status` は自動で `retracted` にしない（反省ループの明示判断で更新）

## 7.4 Intent State

1. `goal_id` / `ticket_id` は再利用しない
2. 状態遷移は `10_基盤ループ.md` の定義を正とする
3. 状態更新時は `reason_code` を必須保存する

## 8. `revisions` 連携規約

1. World Model 更新時は `revisions.entity_type` に `wm_*` を記録する
2. `before_json` / `after_json` を必須保存する
3. `reason` は `reason_code` と一致する短文を保存する
4. 根拠イベントがある更新は `evidence_event_ids_json` を空にしない

## 9. `events` / `state` との接続方針

1. `events -> wm_observations`
   - 永続イベントを Observe で正規化して取り込む
2. `wm_* -> state`
   - この同期規約は Phase 4 で定義する
3. `state -> wm_*`
   - 直接同期しない（重複管理を避ける）
4. `ActionResult -> events`
   - 実行結果は `events` にも残し、会話想起に流す（Phase 4 で詳細化）

## 10. 実装マップ（Phase 6 で実装する対象）

1. `cocoro_ghost/memory_models.py`
   - `wm_*` テーブルモデル追加
2. `cocoro_ghost/db.py`
   - テーブル作成・インデックス作成追加
3. `cocoro_ghost/autonomy/world_model_store.py`
   - `world_model_update_request` の適用処理追加
4. `cocoro_ghost/autonomy/loop_runtime.py`
   - Observe/Reflect から `world_model_update_request` を発行

## 11. 完了条件（Phase 2 仕様完了）

1. `wm_*` の最小テーブル構成が定義されている
2. 主キー/一意制約/必須インデックスが定義されている
3. 競合時の扱い（並存/更新優先/確信度）が定義されている
4. 既存 `events` / `state` / `revisions` との境界が明確化されている
5. Phase 6 実装対象ファイルが特定されている

## 12. 次フェーズへの引き継ぎ

1. Phase 3（`12_capability_registry.md`）へ渡すもの
   - `wm_action_tickets` / `wm_action_results` の schema 前提
   - `reason_code` / `status` 契約
2. Phase 4（`13_event_writeplan連携.md`）へ渡すもの
   - `events -> wm_observations` の取り込み前提
   - `ActionResult -> events/revisions` 連携前提

本書で確定した `wm_*` 境界と競合規約は、後続フェーズで維持する。
