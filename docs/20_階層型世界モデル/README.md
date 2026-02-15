# 階層型世界モデル ドキュメント

このディレクトリは、CocoroGhost の自律人格アーキテクチャ
（Hierarchical World Model）の仕様をまとめる。

## 文書一覧

1. `docs/20_階層型世界モデル/00_概要.md`
   - ゴール、設計原則、共通契約
2. `docs/20_階層型世界モデル/01_実装計画.md`
   - Gate/Phase と完了条件
3. `docs/20_階層型世界モデル/10_基盤ループ.md`
   - 5ループ、状態遷移、reason_code
4. `docs/20_階層型世界モデル/11_world_model.md`
   - `wm_*` 最小スキーマ、競合規約
5. `docs/20_階層型世界モデル/12_capability_registry.md`
   - capability/operation 契約、検証、adapter 規約（`speak` 常設 / `web_access` は最初の外部能力）
6. `docs/20_階層型世界モデル/13_event_writeplan連携.md`
   - command/result 分離、WritePlan 連携
7. `docs/20_階層型世界モデル/14_web_access.md`
   - `web_access` capability 仕様

注記:

- 追加仕様ファイルを作る場合は、実ファイル名をこの一覧へ追記する

## 運用方針

1. 先に `00_概要.md` の共通契約を更新する
2. その後 `10_` 以降の詳細仕様へ反映する
3. docs 間で契約矛盾を残したまま Gate を進めない
