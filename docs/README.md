# cocoro_ghost パートナー最適化仕様（sqlite-vec固定）

このディレクトリは、`cocoro_ghost` を「AIパートナー」として長期運用するための **新仕様（units/payload + Scheduler + Worker + sqlite-vec(vec0) 固定）** を、実装ハンドオフ可能な粒度で分割ドキュメント化したものです。

## 目的

- 人格の一貫性（PersonaAnchor）
- 関係性の連続性（RelationshipContract / SharedNarrative）
- 会話テンポ（同期は軽く、重い処理はWorkerへ）
- 長期運用での“育ち”（Lifecycle: 統合・整理・矛盾管理）

## 前提（固定）

- LLM/Embedding は **API経由**（LiteLLMで切替可能）
- ベクター検索は **sqlite-vec（vec0）で固定**
- ストレージは SQLite（`settings.db` + `memory_<memory_id>.db`）
- MemOSの要点として **Scheduling（予測・プリロード）** と **Lifecycle（統合・整理）** を中核に取り入れる

## ドキュメント一覧（読む順番）

1. `docs/architecture.md`（全体像・データフロー）
2. `docs/settings_db.md`（settings.db / token / presets）
3. `docs/db_schema.md`（DDL / Enum / 永続化ルール）
4. `docs/sqlite_vec.md`（vec0設計・KNN→JOIN）
5. `docs/scheduler.md`（MemoryPack編成・スコア・圧縮）
6. `docs/worker.md`（ジョブ・冪等性・版管理）
7. `docs/prompts.md`（LLM JSONスキーマ）
8. `docs/api.md`（API仕様 / SSE）
9. `docs/bootstrap.md`（初期DB作成）

## 用語（最小）

- **Unit**: すべての記憶の共通メタ（`units` 1行）。
- **Payload**: Unitの種別ごとの本文・構造化情報（`payload_*`）。
- **Canonical / Derived**: 原文・証跡（Canonical）と、要約/事実/ループ等（Derived）を分離。
- **MemoryPack**: LLMへ注入する「固定見出し順」のテキストパック（検索結果の生注入はしない）。
