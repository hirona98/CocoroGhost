# 階層型世界モデル Capability Registry

## 1. 目的

本書は、Capability Registry の仕様を定義する。

## 2. スコープ

対象:

1. `CapabilityDescriptor` / `CapabilityOperationDescriptor`
2. registry 永続化モデル
3. adapter 解決規約
4. input/result/effect schema 検証規約
5. Tacticalize/Execute との接続

非対象:

1. capability 個別仕様（`05_web_access.md`）
2. world model 詳細（`02_world_model.md`）
3. Event/WritePlan 詳細（`04_event_writeplan連携.md`）

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
4. `register_default_capabilities(registry)`
   - 標準 descriptor を再登録する際、既存 capability/operation の `enabled` 状態を保持する

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

1. precondition `operation_available` を先に評価する
2. 不成立なら ticket を `cancelled`（`ticket_precondition_failed`）で確定する
3. 成立後に `resolve_operation` 実行
4. 実行直前に不可用化された場合は `failed`（`execute_adapter_not_found`）
5. `validate_input_payload` 失敗なら `failed`（`execute_schema_invalid`）

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
5. precondition 語彙は `cocoro_ghost/autonomy/preconditions.py` を正とする

### 9.1 precondition 語彙の拡張手順

1. `cocoro_ghost/autonomy/preconditions.py` に定数を追加する
2. `evaluate_preconditions()` に評価ロジックを追加する
3. 必要なら `build_effective_preconditions()` に共通適用ルールを追加する
4. Tacticalize 側（`tactical_planner.py`）で新語彙を参照する
5. `01_基盤ループ.md` と `05_web_access.md` へ語彙追加を反映する

禁止事項:

1. 未定義語彙を ticket に出力しない
2. fallback として「未知語彙を成功扱い」にしない

### 9.2 Tacticalize と Registry の接続規約

1. Tacticalize 出力の `capability_id + operation` は `resolve_operation` 可能であること
2. Tacticalize 出力の `input_payload` は `validate_input_payload` を通ること
3. これらを満たせない計画は Execute へ渡さず `ticket_precondition_failed` で確定すること

## 10. Adapter 規約

1. adapter は capability 単位で実装してよい
2. operation 分岐は adapter 内で実装してよい
3. adapter は `result_payload` と `effects` を必ず返す
4. adapter は DB を直接更新しない
5. 副作用を実行した場合も必ず `ActionResult` と `effects` に反映する

## 11. capability 追加手順（能力非依存）

### 11.1 実施順（一本道）

1. 入力契約を決める
   - `capability_id`
   - operation 一覧
   - 各 operation の `input_schema_json` / `result_schema_json` / `effect_schema_json`
2. adapter を実装する
   - `cocoro_ghost/autonomy/capability_adapters/<capability_id>.py`
   - `execute_operation()` で operation 分岐を実装する
   - 返り値は必ず `AdapterExecutionOutput(result_payload, effects, meta_json)` を返す
3. adapter 公開面を更新する
   - `cocoro_ghost/autonomy/capability_adapters/__init__.py`
4. descriptor を登録する
   - `cocoro_ghost/autonomy/capability_bootstrap.py`
   - `register_default_capabilities()` へ capability と operation を追加する
   - 再登録時の `enabled` 維持規約（既存状態優先）を守る
5. Tacticalize 接続を実装する
   - `cocoro_ghost/autonomy/tactical_planner.py`
   - `TacticalPlanContract` で `capability_id + operation + input_payload + preconditions` を出力する
6. precondition 語彙を必要に応じて拡張する
   - `cocoro_ghost/autonomy/preconditions.py`
   - `normalize_preconditions` / `build_effective_preconditions` / `evaluate_preconditions` を同時に更新する
7. effect 反映を実装する
   - `cocoro_ghost/autonomy/effect_reflector.py`
   - `effects` から `wm_*` への反映規則を追加する
8. 仕様書を更新する
   - `docs/20_階層型世界モデル/03_capability_registry.md`
   - 必要に応じて capability 個別仕様（例: `docs/20_階層型世界モデル/05_web_access.md` と同形式の新規ファイル）を追加する
9. 手動確認を実施する
   - registry 解決、schema 検証、execute、reflect までの一連経路を確認する

### 11.2 変更ファイル一覧（標準）

必須変更:

1. `cocoro_ghost/autonomy/capability_adapters/<capability_id>.py`
2. `cocoro_ghost/autonomy/capability_adapters/__init__.py`
3. `cocoro_ghost/autonomy/capability_bootstrap.py`
4. `cocoro_ghost/autonomy/tactical_planner.py`
5. `cocoro_ghost/autonomy/effect_reflector.py`
6. `docs/20_階層型世界モデル/03_capability_registry.md`

条件付き変更:

1. `cocoro_ghost/autonomy/preconditions.py`（新語彙を導入する場合）
2. `docs/20_階層型世界モデル/01_基盤ループ.md`（precondition 語彙一覧を更新する場合）
3. `docs/20_階層型世界モデル/05_web_access.md` と同等の capability 個別仕様書（新 capability を追加した場合）

変更禁止:

1. `cocoro_ghost/autonomy/loop_runtime.py` に capability 固有 if を追加しない
2. `cocoro_ghost/worker_handlers.py` に capability 個別分岐を追加しない
3. fallback 専用経路を追加しない

### 11.3 完了チェックリスト（Phase 10 受け入れ条件）

1. `capability_id + operation` が `resolve_operation()` で解決できる
2. Tacticalize 出力 `input_payload` が `validate_input_payload()` を通る
3. adapter 実行結果が `validate_result_payload()` / `validate_effects()` を通る
4. Reflect で `effects` が `wm_*` に反映される
5. `loop_runtime` 非改変で end-to-end 実行できる
6. 仕様書更新が完了している
