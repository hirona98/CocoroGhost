# 階層型世界モデル Capability Registry

## 1. 目的

本書は、階層型世界モデルにおける Capability Registry の仕様を定義する。
Phase 3 の目的は次の2点。

1. `CapabilityDescriptor` の登録/解決/検証契約を固定する
2. capability 追加時に基盤ロジック改修が不要であることを仕様で担保する

## 2. スコープ

対象:

1. `CapabilityDescriptor` / operation 契約
2. registry の永続化モデル
3. adapter 呼び出し規約
4. 入出力/effect の schema 検証規約
5. 基盤ループ（Tacticalize/Execute）との接続

非対象:

1. `web_access` の個別 operation 仕様（Phase 5）
2. World Model 永続化仕様（Phase 2）
3. Event/WritePlan 連携詳細（Phase 4）
4. 実コード実装（Phase 6）

## 3. 前提

1. capability 一覧は固定しない
2. `/api/chat` の `reply_web_search_enabled` と自律 capability は別制御
3. fail-fast を正とし、fallback は行わない
4. 基盤ループ本体に capability 固有分岐を持ち込まない

## 4. 論理構成

```text
[wm_capabilities / wm_capability_operations]
    -> [CapabilityRegistry]
    -> [CapabilityAdapterResolver]
    -> [Execute Loop]
```

構成要素:

1. Descriptor Store
   - capability と operation の定義を保持する
2. CapabilityRegistry
   - 登録/有効化状態/解決を担当する
3. AdapterResolver
   - `capability_id` + `operation` から adapter を返す
4. Schema Validator
   - input/result/effect の JSON Schema 検証を担当する

## 5. データ契約

## 5.1 CapabilityDescriptor（能力単位）

```json
{
  "capability_id": "string",
  "display_name": "string",
  "enabled": true,
  "operations": ["string"],
  "version": "1",
  "metadata_json": {}
}
```

ルール:

1. `capability_id` はグローバル一意
2. `enabled=false` の capability は Tacticalize で候補にしない
3. `operations` は1件以上

## 5.2 CapabilityOperationDescriptor（operation 単位）

```json
{
  "capability_id": "string",
  "operation": "string",
  "input_schema_json": {},
  "result_schema_json": {},
  "effect_schema_json": {},
  "timeout_seconds": 30,
  "enabled": true
}
```

ルール:

1. `capability_id + operation` は一意
2. `input_schema_json` は Execute 前に検証する
3. `result_schema_json` / `effect_schema_json` は Execute 後に検証する
4. operation 側が `enabled=false` の場合、その operation は解決不可とする

## 5.3 AdapterExecutionContext

```json
{
  "goal_id": "uuid",
  "ticket_id": "uuid",
  "capability_id": "string",
  "operation": "string",
  "trigger_type": "event_created|action_result|periodic|startup",
  "issued_at": "2026-02-15T12:00:00"
}
```

## 5.4 AdapterExecutionOutput

```json
{
  "result_payload": {},
  "effects": [],
  "meta_json": {}
}
```

## 6. 永続化モデル（`memory_<embedding_preset_id>.db`）

## 6.1 `wm_capabilities`

主要カラム:

1. `capability_id` TEXT PK
2. `display_name` TEXT NOT NULL
3. `enabled` INTEGER NOT NULL（0/1）
4. `version` TEXT NOT NULL
5. `metadata_json` TEXT NOT NULL（default `"{}"`）
6. `created_at` INTEGER NOT NULL
7. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_capabilities_enabled` (`enabled`)

## 6.2 `wm_capability_operations`

主要カラム:

1. `id` INTEGER PK
2. `capability_id` TEXT NOT NULL（FK -> `wm_capabilities.capability_id`）
3. `operation` TEXT NOT NULL
4. `input_schema_json` TEXT NOT NULL
5. `result_schema_json` TEXT NOT NULL
6. `effect_schema_json` TEXT NOT NULL
7. `timeout_seconds` INTEGER NOT NULL
8. `enabled` INTEGER NOT NULL（0/1）
9. `created_at` INTEGER NOT NULL
10. `updated_at` INTEGER NOT NULL

制約:

1. UNIQUE（`capability_id`, `operation`）

必須インデックス:

1. `idx_wm_cap_ops_capability_enabled` (`capability_id`, `enabled`)
2. `idx_wm_cap_ops_operation` (`operation`)

## 7. Registry API 契約

## 7.1 登録系

1. `register_capability(descriptor)`
   - capability と operation 群を upsert する
2. `set_capability_enabled(capability_id, enabled)`
   - capability 全体の有効/無効を切り替える
3. `set_operation_enabled(capability_id, operation, enabled)`
   - operation 単位の有効/無効を切り替える

## 7.2 参照系

1. `resolve(capability_id, operation)`
   - descriptor と adapter ハンドルを返す
2. `list_enabled_capabilities()`
   - Tacticalize が参照する有効 capability 一覧を返す
3. `get_operation_descriptor(capability_id, operation)`
   - schema 検証用の operation 定義を返す

## 7.3 検証系

1. `validate_input(capability_id, operation, input_payload)`
2. `validate_result(capability_id, operation, result_payload)`
3. `validate_effect(capability_id, operation, effects)`

## 8. Execute ループでの適用規約

## 8.1 実行前

1. `resolve(capability_id, operation)` を実行
2. 解決できなければ `failed`（`reason_code=execute_adapter_not_found`）
3. `validate_input` に失敗したら `failed`（`reason_code=execute_schema_invalid`）

## 8.2 実行中

1. adapter を1回だけ実行する（内部 fallback/retry なし）
2. timeout 超過は実行失敗として扱う（`reason_code=execute_runtime_error`）

## 8.3 実行後

1. `validate_result` を実行
2. `validate_effect` を実行
3. どちらか失敗したら `failed`（`reason_code=execute_schema_invalid`）

## 9. Tacticalize ループでの適用規約

1. `enabled=true` の capability / operation のみ候補にする
2. 無効 capability しか使えない goal は `paused` 候補にする
3. operation 未定義 ticket は発行しない
4. capability 不可用時は `preconditions` 不成立として扱う

## 10. Adapter 規約

1. adapter は `capability_id` ごとに実装する
2. adapter は `operation` を分岐して実行してよい
3. adapter は `result_payload` と `effects` を必ず返す
4. adapter は DB を直接更新しない（更新はループ側で集約）
5. adapter は capability 固有の副作用を実行してよいが、
   結果は必ず `ActionResult` と `effects` に反映する

## 11. capability 追加手順（能力非依存）

1. adapter を実装する
2. `CapabilityDescriptor` と operation schema を定義する
3. registry へ登録する
4. 既存基盤ループ（Observe/Strategize/Tacticalize/Execute/Reflect）を変更せずに実行する

禁止事項:

1. `loop_runtime` に capability 固有 `if` を追加しない
2. `worker_handlers.py` に capability 個別分岐を追加しない
3. fallback 専用経路を追加しない

## 12. 初期セット（Phase 6）

1. `speak` のみを登録する
2. `web_access` は Phase 7 で追加する
3. `speak` も registry 経由で解決する（特別扱いしない）

## 13. 実装マップ（Phase 6 で実装する対象）

1. `cocoro_ghost/memory_models.py`
   - `wm_capabilities` / `wm_capability_operations` を追加
2. `cocoro_ghost/db.py`
   - テーブル作成・インデックス作成を追加
3. `cocoro_ghost/autonomy/capability_registry.py`
   - registry API を実装
4. `cocoro_ghost/autonomy/capability_adapters/base.py`
   - adapter 共通契約を実装
5. `cocoro_ghost/autonomy/capability_adapters/speak.py`
   - `speak` adapter を実装
6. `cocoro_ghost/autonomy/loop_runtime.py`
   - Tacticalize/Execute から registry を参照

## 14. 完了条件（Phase 3 仕様完了）

1. capability の登録/解決/検証契約が定義されている
2. operation ごとの schema 検証規約が定義されている
3. adapter 呼び出し規約が定義されている
4. capability 追加手順が能力非依存で定義されている
5. 追加時に基盤ロジック改修不要であることが明記されている

## 15. 次フェーズへの引き継ぎ

1. Phase 4（`13_event_writeplan連携.md`）へ渡すもの
   - `ActionResult.effects` の保存・配信前提
   - `reason_code` のエラー連携前提
2. Phase 5（`14_web_access.md`）へ渡すもの
   - `web_access` を descriptor 追加のみで組み込む前提
   - operation schema 駆動で定義する前提

本書で確定した registry 契約は、後続フェーズでも維持する。
