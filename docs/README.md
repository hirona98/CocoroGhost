# cocoro_ghost 仕様

このディレクトリは、`cocoro_ghost` の設計/仕様（units/payload + Scheduler + Worker + sqlite-vec(vec0)）を、実装ハンドオフ可能な粒度で分割ドキュメント化したものです。

## 目的

- 人格の一貫性（PersonaAnchor）
- 関係性の連続性（RelationshipContract / SharedNarrative）
- 会話テンポ（同期は軽く、重い処理はWorkerへ）
- 長期運用での“育ち”（Lifecycle: 統合・整理・矛盾管理）

## 前提

- LLM/Embedding は **API経由**（LiteLLMで切替可能）
- ベクター検索は **sqlite-vec（vec0）** を使用する
- ストレージは SQLite（`settings.db` + `memory_<memory_id>.db`）
- MemOSの要点として **Scheduling（予測・プリロード）** と **Lifecycle（統合・整理）** を中核に取り入れる

## ドキュメント一覧（読む順番）

1. `docs/architecture.md`（全体像・データフロー）
2. `docs/settings_db.md`（settings.db / token / presets）
3. `docs/db_schema.md`（DDL / Enum / 永続化ルール）
4. `docs/sqlite_vec.md`（vec0設計・KNN→JOIN）
5. `docs/scheduler.md`（MemoryPack編成・スコア・圧縮）
6. `docs/retrieval.md`（文脈考慮型記憶検索・Intent廃止）
6.1. `docs/retrieval_fastpath.md`（Retriever高速化: LLMレス改造計画）
7. `docs/worker.md`（ジョブ・冪等性・版管理）
8. `docs/prompts.md`（LLM JSONスキーマ）
9. `docs/api.md`（API仕様 / SSE）
10. `docs/bootstrap.md`（初期DB作成）

## 用語

- **Unit**: 記憶DB（`memory_<memory_id>.db`）で扱う「1つの記憶/出来事/生成物」の基本単位。共通メタを `units` に1行で持ち、`kind/state/sensitivity/pin/topic_tags` 等で扱いを決める（詳細は `docs/db_schema.md`）。
- **Payload**: Unitの本文や構造化データを、種別ごとにスキーマ分離したテーブル群（`payload_episode` / `payload_fact` / `payload_summary` / `payload_loop` / `payload_capsule` など）。Unitと同じ `unit_id` で1:1に紐づく（詳細は `docs/db_schema.md`）。
- **UnitKind / UnitState / Sensitivity**: Unitの「種別」「状態」「取り扱い区分」をenum値で表すもの。検索・注入・Worker処理の対象範囲を決めるための土台（詳細は `docs/db_schema.md` と実装の `cocoro_ghost/unit_enums.py`）。
- **Canonical / Derived**: “原文（証跡）” と “派生物” を分ける考え方。
  - Canonical: ユーザー発話や通知本文など「改変しないログ」（例: `EPISODE`）。
  - Derived: Workerで抽出/統合された「解釈・要約・構造化」（例: `FACT` / `SUMMARY` / `LOOP` / `CAPSULE`）。
- **MemoryPack**: `/api/chat` の同期処理中に、Schedulerが「LLMへ注入するため」に組み立てるテキストパック。見出し順（`[PERSONA_ANCHOR]` 等）に沿って、検索結果をそのまま貼らずに圧縮・整形する（仕様: `docs/scheduler.md`、実装: `cocoro_ghost/scheduler.py`）。
- **Retriever**: 文脈考慮型の記憶検索システム。Query Expansion → Contextual KNN → LLM Reranking の3段階で、会話に関連する過去のエピソードを選別する（仕様: `docs/retrieval.md`）。従来の Intent 分類を廃止し、常時・文脈考慮型の検索に移行。
- **Persona / Contract**: LLM注入プロンプトを「人格」と「関係契約」に分けたもの。
  - Persona: 人格・口調・価値観の中核（崩れると会話の一貫性が壊れる）。
  - Contract: 踏み込み/介入の許可、NG、距離感、取り扱い注意などの「関係契約」。
  - 注入上は、Persona/Contract は MemoryPack の先頭セクションに含める（`docs/scheduler.md` / `docs/api.md`）。
  - 内部注入コンテキストの取り扱い（秘匿/ガード）はコード側の固定プロンプトで行う（実装: `cocoro_ghost/memory.py`）。
- **Preset（settings）**: `settings.db` に永続化する切替単位。
  - LLM/Embeddingの接続情報・検索予算に加え、Persona/Contract をプリセットとして保持し、`active_*_preset_id` でアクティブを選ぶ（`docs/settings_db.md`）。
- **memory_id**: 記憶DBファイル名を選ぶための識別子。`EmbeddingPreset.id`（UUID）を `memory_id` として扱い、`memory_<memory_id>.db` を開く（`docs/settings_db.md` / `docs/api.md`）。
