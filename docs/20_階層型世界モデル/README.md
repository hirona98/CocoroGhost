# 階層型世界モデル ドキュメント

このディレクトリは、CocoroGhost の自律人格アーキテクチャ
（Hierarchical World Model）の仕様をまとめる。

## 文書一覧

1. `docs/20_階層型世界モデル/00_概要.md`
   - ゴール、設計原則、共通契約
2. `docs/20_階層型世界モデル/01_基盤ループ.md`
   - 5ループ、状態遷移、reason_code
3. `docs/20_階層型世界モデル/02_world_model.md`
   - `wm_*` 最小スキーマ、競合規約
4. `docs/20_階層型世界モデル/03_capability_registry.md`
   - capability/operation 契約、検証、adapter 規約（`speak` 常設 / `web_access` は最初の外部能力）
5. `docs/20_階層型世界モデル/04_event_writeplan連携.md`
   - command/result 分離、WritePlan 連携
6. `docs/20_階層型世界モデル/05_web_access.md`
   - `web_access` capability 仕様

注記:

- 追加仕様ファイルを作る場合は、実ファイル名をこの一覧へ追記する

## 運用方針

1. 先に `00_概要.md` の共通契約を更新する
2. その後 `01_` 以降の詳細仕様へ反映する
3. docs 間で契約矛盾を残したまま Gate を進めない
4. 進捗情報は本書の「実装状況」に集約し、他仕様書へ分散させない

## 実装状況（2026-02-15）

完了:

1. Phase 1: 基盤ループ仕様
2. Phase 2: World Model 仕様
3. Phase 3: Capability Registry 仕様
4. Phase 4: Event/WritePlan 連携仕様
5. Phase 5: `web_access` 仕様
6. Phase 6: 基盤実装
7. Phase 7: `web_access` 実装
8. Phase 8: 横断動作確認
9. Phase 9: Tacticalize 規約拡張
10. Phase 10: 次 capability 追加の受け入れ準備

次の実行順:

1. 新 capability の機能設計に着手
2. 追加対象 capability ごとに Phase を起票し、本書へ追記
