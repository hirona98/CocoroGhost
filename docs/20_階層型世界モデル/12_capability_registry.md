# 階層型世界モデル Capability Registry

## 1. 目的

本書は、Capability Registry の仕様を定義する。

Phase 3 の目的:

1. capability/operation の登録・解決・検証契約を固定する
2. capability 追加時に基盤ループ改修不要であることを担保する

## 2. スコープ

対象:

1. `CapabilityDescriptor` / `CapabilityOperationDescriptor`
2. registry 永続化モデル
3. adapter 解決規約
4. input/result/effect schema 検証規約
5. Tacticalize/Execute との接続

非対象:

1. capability 個別仕様（`14_web_access.md`）
2. world model 詳細（`11_world_model.md`）
3. Event/WritePlan 詳細（`13_event_writeplan連携.md`）

## 3. 前提

1. capability 一覧は固定しない
2. `/api/chat` の `reply_web_search_enabled` とは別制御
3. fail-fast を正とする
4. fallback を導入しない
5. 基盤ループ本体へ capability 固有分岐を持ち込まない

## 4. 論理構成

```text
[wm_capabilities / wm_capability_operations]
    -> [CapabilityRegistry]
    -> [AdapterResolver]
    -> [Execute Loop]
```

## 5. データ契約

### 5.1 CapabilityDescriptor（能力単位）

```json
{
  "capability_id": "string",
  "display_name": "string",
  "enabled": true,
  "version": "1",
  "metadata_json": {},
  "operations": ["string"]
}
```

ルール:

1. `capability_id` はグローバル一意
2. `enabled=false` capability は Tacticalize 候補にしない
3. `operations` は1件以上

### 5.2 CapabilityOperationDescriptor（operation 単位）

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
2. `input_schema_json` / `result_schema_json` / `effect_schema_json` は JSON Schema Draft 2020-12 で定義する
3. schema は top-level `type=object` かつ `additionalProperties=false` を必須にする
4. input schema は Execute 前に検証
5. result/effect schema は Execute 後に検証
6. `enabled=false` operation は解決不可

### 5.3 AdapterExecutionContext

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

### 5.4 AdapterExecutionOutput

```json
{
  "result_payload": {},
  "effects": [],
  "meta_json": {}
}
```

## 6. 永続化モデル（`memory_<embedding_preset_id>.db`）

### 6.1 `wm_capabilities`

主要カラム:

1. `capability_id` TEXT PK
2. `display_name` TEXT NOT NULL
3. `enabled` INTEGER NOT NULL（0/1）
4. `version` TEXT NOT NULL
5. `metadata_json` TEXT NOT NULL（default `{}`）
6. `created_at` INTEGER NOT NULL
7. `updated_at` INTEGER NOT NULL

必須インデックス:

1. `idx_wm_capabilities_enabled` (`enabled`)

### 6.2 `wm_capability_operations`

主要カラム:

1. `operation_id` INTEGER PK
2. `capability_id` TEXT NOT NULL
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

### 7.1 登録系

1. `register_descriptor(descriptor)`
   - capability と operation を upsert
2. `set_capability_enabled(capability_id, enabled)`
   - capability 全体を有効/無効化
3. `set_operation_enabled(capability_id, operation, enabled)`
   - operation 単位を有効/無効化

### 7.2 参照系

1. `resolve_operation(capability_id, operation)`
   - operation descriptor を返す
2. `list_enabled_capabilities()`
   - Tacticalize 用の候補一覧を返す
3. `get_operation_descriptor(capability_id, operation)`
   - schema 検証用 descriptor を返す

### 7.3 検証系

1. `validate_input_payload(capability_id, operation, payload)`
2. `validate_result_payload(capability_id, operation, payload)`
3. `validate_effects(capability_id, operation, effects)`

## 8. Execute 適用規約

### 8.1 実行前

1. `resolve_operation` 実行
2. 未解決なら `failed`（`execute_adapter_not_found`）
3. `validate_input_payload` 失敗なら `failed`（`execute_schema_invalid`）

### 8.2 実行中

1. adapter は1回のみ実行（内部 retry/fallback なし）
2. timeout は `failed`（`execute_runtime_error`）

### 8.3 実行後

1. `validate_result_payload` 実行
2. `validate_effects` 実行
3. 失敗時は `failed`（`execute_schema_invalid`）

## 9. Tacticalize 適用規約

1. `enabled=true` capability / operation のみ候補
2. 無効 capability しか使えない goal は `paused` 候補
3. 未定義 operation ticket は発行しない
4. capability 不可用時は precondition 未成立として扱う

## 10. Adapter 規約

1. adapter は capability 単位で実装してよい
2. operation 分岐は adapter 内で実装してよい
3. adapter は `result_payload` と `effects` を必ず返す
4. adapter は DB を直接更新しない
5. 副作用を実行した場合も必ず `ActionResult` と `effects` に反映する

## 11. capability 追加手順（能力非依存）

1. adapter 実装
2. descriptor/schema 定義
3. registry 登録
4. 既存基盤ループ変更なしで実行

禁止事項:

1. `loop_runtime` に capability 固有 if を追加しない
2. `worker_handlers.py` に capability 個別分岐を追加しない
3. fallback 専用経路を追加しない

## 12. 初期セット（あるべき姿）

1. Phase 6 では registry 共通機構と内部基本 capability `speak`（descriptor + adapter）を実装する
2. `speak` は環境依存しない最小出力能力として常設する
3. 最初の外部 capability は `web_access` とする
4. Phase 7 で `web_access` descriptor + adapter を登録する

## 13. 実装マップ（Phase 6）

1. `cocoro_ghost/memory_models.py`
   - `wm_capabilities` / `wm_capability_operations`
2. `cocoro_ghost/db.py`
   - テーブル・インデックス作成
3. `cocoro_ghost/autonomy/capability_registry.py`
   - registry API
4. `cocoro_ghost/autonomy/capability_adapters/base.py`
   - adapter 共通契約
5. `cocoro_ghost/autonomy/capability_adapters/speak.py`
   - `speak` adapter
6. `cocoro_ghost/autonomy/loop_runtime.py`
   - Tacticalize/Execute から registry 利用

## 14. 完了条件（Phase 3）

1. 登録/解決/検証契約が固定されている
2. operation 単位 schema 検証規約が固定されている
3. adapter 呼び出し規約が固定されている
4. capability 追加手順が能力非依存で固定されている
