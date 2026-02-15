# 階層型世界モデル Event/WritePlan連携

## 1. 目的

本書は、階層型世界モデルの行動・観測・反省を既存記憶基盤（`events` / `WritePlan`）へ接続する仕様を定義する。
Phase 4 の目的は次の2点。

1. 非永続化命令イベントと永続化結果イベントの切り分けを固定する
2. 既存 `/api/chat` 同期経路と競合しない連携方式を固定する

## 2. スコープ

対象:

1. 自律ループのイベント分類（命令/結果）
2. `events` 保存規約（source、本文、配信）
3. WritePlan ジョブ投入規約
4. `ActionResult` / `world_model_update_request` と `events` の接続順序
5. 失敗時の再試行・観測規約

非対象:

1. World Model テーブル定義の詳細（Phase 2）
2. Capability Registry 詳細（Phase 3）
3. `web_access` 個別仕様（Phase 5）
4. 実コード実装（Phase 6）

## 3. 前提

1. `/api/chat` の同期経路に重い処理を混ぜない
2. 自律処理は worker/periodic 側で実行する
3. fail-fast を正とし、fallback は入れない
4. 命令イベントは非永続化可、結果イベントは永続化必須

## 4. イベント分類

## 4.1 非永続化命令イベント（Command Event）

定義:

- クライアント/デバイスへ「実行要求」を送るためのイベント
- `events` テーブルへ保存しない
- `events/stream` で配信し、`event_id=0` とする

代表例:

1. `vision.capture_request`
2. 今後のデバイス命令（移動/家電制御等）の要求イベント

ルール:

1. command event は WritePlan 対象にしない
2. command event の成功/失敗判定は response 側で行う

## 4.2 永続化結果イベント（Result Event）

定義:

- 実行結果・観測結果・自発発話の記録イベント
- `events` テーブルへ保存する
- 必要に応じて `events/stream` へ配信する

対象 source（Phase 6で扱う最小セット）:

1. `meta_proactive`（自発発話）
2. `vision_detail`（視覚結果の説明）
3. `desktop_watch`（能動観測結果）
4. `notification` / `reminder`（既存外部入力）
5. `autonomy_action`（自律実行結果の要約。Phase 6 で追加）

ルール:

1. result event は必ず `event_id>0` で永続化する
2. result event は必ず WritePlan 入力へ流す

## 5. `events` 保存規約

## 5.1 共通フィールド

1. `created_at` / `updated_at`: 現在UTC秒
2. `source`: 上記 source のいずれか
3. `entities_json`: 初期値は `"[]"`（後段 WritePlan で更新）
4. `client_context_json`: 取得できる場合のみ格納

## 5.2 自発発話（`operation=speak`）

1. `source="meta_proactive"`
2. `assistant_text` に発話本文を入れる
3. `user_text` は `null` でよい
4. `events/stream` へ配信する

## 5.3 非発話アクション結果（`autonomy_action`）

1. `source="autonomy_action"`
2. `user_text` に観測サマリ（何を実行し何が起きたか）を入れる
3. `assistant_text` はユーザー共有文がある場合のみ入れる
4. `events/stream` 配信は `ticket` の可視性ポリシーに従う

## 5.4 視覚命令の扱い

1. `vision.capture_request` は非永続化（`event_id=0`）
2. `vision.capture_response` の結果説明は `source="vision_detail"` として永続化
3. `vision_detail` はUI通知不要なら配信しない（既存方針を維持）

## 6. WritePlan 連携規約

## 6.1 ジョブ投入規約

result event 保存後、同一経路で次のジョブを投入する。

1. `upsert_event_embedding`
2. `upsert_event_assistant_summary`
3. `generate_write_plan`

注記:

1. `assistant_text` が空でも `upsert_event_assistant_summary` は投入してよい（実処理側で判断）
2. 既存 chat/external 経路と同じジョブチェーンを使う

## 6.2 WritePlan 出力の扱い

1. `event_annotations` は `events` 注釈へ反映する
2. `state_updates` / `preference_updates` は既存 `state` / `user_preferences` 更新へ反映する
3. `event_affect` は `event_affects` へ反映する
4. 更新差分は `revisions` に記録する

## 6.3 自律ループとの役割分担

1. World Model（`wm_*`）更新:
   - `world_model_update_request` で反映する
2. 会話向け記憶（`state` 等）更新:
   - WritePlan で反映する
3. 二重管理を避けるため、`state -> wm_*` の直接同期は行わない

## 7. 実行順序（1 ticket 実行後）

1. Execute で `ActionResult` を確定する
2. Reflect 前後で `world_model_update_request` を発行する
3. 結果を `events` に永続化する（必要なら配信）
4. WritePlan ジョブを投入する
5. 非同期 worker が既存 `generate_write_plan -> apply_write_plan` を実行する

ルール:

1. `/api/chat` 同期経路でこの処理を待たない
2. 連携処理はすべて worker 経路で完結させる

## 8. `events/stream` 配信規約

## 8.1 配信するもの

1. ユーザーに見せるべき result event
2. command event（`event_id=0`）でクライアント実行が必要なもの

## 8.2 配信しないもの

1. 内部観測だけで十分な result event（例: 一部 `vision_detail`）
2. WritePlan の中間生成物

## 8.3 宛先

1. 対象クライアントが必要な command/result は `target_client_id` 指定
2. 全体通知は broadcast（`target_client_id=None`）

## 9. エラー/再試行規約

1. result event 保存失敗:
   - 当該 cycle を失敗として終了し、worker 再試行に委譲
2. WritePlan ジョブ投入失敗:
   - cycle は失敗として扱う（WritePlan 欠落を許容しない）
3. WritePlan 実行失敗:
   - 既存 worker retry ポリシー（`jobs`）に従う
4. command event 応答なし:
   - timeout として `ActionResult.failed` を確定する

## 10. 観測項目

1. command event 配信件数（type別）
2. result event 永続化件数（source別）
3. WritePlan ジョブ投入件数（kind別）
4. 自律起点 event の WritePlan 成功/失敗件数
5. `event_id=0` 命令の timeout 件数

## 11. 実装マップ（Phase 6 で実装する対象）

1. `cocoro_ghost/memory/_jobs_mixin.py`
   - 自律結果event向けのジョブ投入ヘルパを追加
2. `cocoro_ghost/autonomy/loop_runtime.py`
   - result event 永続化と WritePlan ジョブ投入を接続
3. `cocoro_ghost/worker_handlers_autonomy.py`
   - cycle 内の連携実行を呼び出し
4. `cocoro_ghost/memory_models.py`
   - `events.source="autonomy_action"` の運用追加
5. `cocoro_ghost/event_stream.py`
   - command/result 配信契約を維持（`event_id=0` 命令）

## 12. 完了条件（Phase 4 仕様完了）

1. 非永続化命令イベントと永続化結果イベントの切り分けが定義されている
2. result event から WritePlan へ流す規約が定義されている
3. `world_model_update_request` と WritePlan の役割分担が定義されている
4. `/api/chat` 同期経路と競合しないことが明記されている
5. `docs/10_実行フロー.md` と矛盾しない連携方式になっている

## 13. 次フェーズへの引き継ぎ

1. Gate 1 へ渡すもの
   - Phase 1-4 の連携整合（ループ/World Model/Registry/WritePlan）
2. Phase 5（`14_web_access.md`）へ渡すもの
   - `web_access` 結果は result event 化して同一WritePlan経路へ流す前提
   - `web_access` の command/result を本書のイベント分類に従わせる前提

本書で確定した Event/WritePlan 連携規約は、後続フェーズでも維持する。
