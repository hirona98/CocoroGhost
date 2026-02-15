# 階層型世界モデル Event/WritePlan連携

## 1. 目的

本書は、自律ループの行動・観測・反省を既存記憶基盤（`events` / WritePlan）へ接続する仕様を定義する。

## 2. スコープ

対象:

1. 自律ループ起点イベント分類
2. `events` 保存規約
3. result event から WritePlan へのジョブ投入規約
4. `ActionResult` / `world_model_update_request` / `events` の接続順序

非対象:

1. world model 詳細（`02_world_model.md`）
2. registry 詳細（`03_capability_registry.md`）
3. capability 個別仕様（`05_web_access.md`）

## 3. 前提

1. `/api/chat` 同期経路に重い処理を混ぜない
2. 自律処理は worker/periodic 側で実行する
3. fail-fast を正とし fallback を入れない
4. command event は非永続、result event は永続必須

## 4. イベント分類

### 4.1 非永続 command event

定義:

- クライアント/デバイスへの実行要求イベント
- `events` テーブルへ保存しない
- `events/stream` へ `event_id=0` で配信する

代表例:

1. `vision.capture_request`
2. 将来のデバイス命令（移動/家電操作）

ルール:

1. command event は WritePlan 対象外
2. 成功/失敗は対応する result event で確定する

### 4.2 永続 result event

定義:

- 実行結果・観測結果・自発発話の記録
- `events` テーブルへ保存する
- 必要時のみ `events/stream` へ配信する

対象 source（最小）:

1. `meta_proactive`
2. `vision_detail`
3. `desktop_watch`
4. `notification` / `reminder`
5. `autonomy_action`

ルール:

1. result event は `event_id>0` で永続化
2. result event は必ず WritePlan 入力へ流す

## 5. `events` 保存規約

### 5.1 共通フィールド

1. `source`: 上記 source のいずれか
2. `created_at` / `updated_at`: UTC UNIX 秒
3. `entities_json`: 初期は `[]`（後段で更新）
4. `client_context_json`: 取得できた場合のみ保存

### 5.2 自発発話（`operation=speak`）

注記:

- `speak` は内部基本 capability（常設）として registry 経由で実行する

1. `source="meta_proactive"`
2. `assistant_text` に発話本文
3. `user_text` は `null`
4. 必要時のみ `events/stream` へ配信

### 5.3 非発話アクション結果

1. `source="autonomy_action"`
2. `user_text` に観測サマリ
3. `assistant_text` はユーザー共有文がある場合のみ
4. 配信有無は ticket 可視性ポリシーで決定

### 5.4 視覚命令

1. `vision.capture_request` は非永続（`event_id=0`）
2. `vision.capture_response` の説明は `source="vision_detail"` として永続化
3. UI通知不要の `vision_detail` は配信しない

## 6. WritePlan 連携規約

### 6.1 ジョブ投入順序

result event 保存後、同一経路で以下を投入する。

1. `upsert_event_embedding`
2. `upsert_event_assistant_summary`
3. `generate_write_plan`

注記:

1. `assistant_text` が空でも `upsert_event_assistant_summary` は投入可
2. 既存 chat/external と同一ジョブチェーンを使う

### 6.2 WritePlan 出力の反映

1. `event_annotations` -> `events` 注釈
2. `state_updates` / `preference_updates` -> `state` / `user_preferences`
3. `event_affect` -> `event_affects`
4. 差分 -> `revisions`

### 6.3 役割分担

1. 自律判断用構造状態は `wm_*` に反映
2. 会話向け記憶更新は WritePlan で反映
3. `state -> wm_*` の直接同期は行わない

## 7. 実行順序（1 ticket 完了時）

1. Execute で `ActionResult` 確定
2. Reflect で `world_model_update_request` 発行・適用
3. result event を `events` へ保存（必要時配信）
4. WritePlan ジョブ投入
5. worker が `generate_write_plan -> apply_write_plan` を実行

ルール:

1. `/api/chat` 同期経路で待たない
2. 連携処理は worker 経路で完結させる

## 8. `events/stream` 配信規約

### 8.1 配信対象

1. ユーザーへ可視化すべき result event
2. クライアント実行が必要な command event（`event_id=0`）

### 8.2 非配信対象

1. 内部観測のみで十分な result event
2. WritePlan 中間生成物

### 8.3 宛先

1. 対象クライアント指定時は `target_client_id` を使う
2. 全体通知は broadcast（`target_client_id=null`）

## 9. エラー/再試行規約

1. result event 保存失敗
   - cycle を失敗として終了し worker retry へ委譲
2. WritePlan ジョブ投入失敗
   - cycle を失敗として扱う
3. WritePlan 実行失敗
   - 既存 worker retry ポリシーに従う
4. command event 応答なし
   - timeout として `ActionResult.failed` を確定

## 10. 観測項目

1. command event 配信件数（type別）
2. result event 永続化件数（source別）
3. WritePlan ジョブ投入件数（kind別）
4. 自律起点 event の WritePlan 成功/失敗件数
5. `event_id=0` 命令 timeout 件数
