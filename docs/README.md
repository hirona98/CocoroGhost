# cocoro_ghost 仕様

このディレクトリは、`cocoro_ghost` の設計/仕様（units/payload + MemoryPack Builder + Worker + sqlite-vec(vec0)）を、実装ハンドオフ可能な粒度で分割ドキュメント化したものです。

## 目的

- 人格の一貫性（PersonaAnchor）
- 関係性の連続性（SharedNarrative）
- 会話テンポ（同期は軽く、重い処理はWorkerへ）
- 長期運用での継続的な統合・更新（Lifecycle: 統合・整理・矛盾管理）

## 前提

- LLM/Embedding は **API経由**（LiteLLMで切替可能）
- ベクター検索は **sqlite-vec（vec0）** を使用する
- ストレージは SQLite（`settings.db` + `memory_<embedding_preset_id>.db`）
- **Scheduling（予測・プリロード）** と **Lifecycle（統合・整理）** を中核に取り入れる

## 制約 / Non-goals（現状の割り切り）

- **uvicorn の multi-worker（複数プロセス）は前提にしない**: 内蔵Workerが同一DBの jobs を重複実行しうるため、`workers=1` 前提で運用する（`docs/worker.md` / `docs/memory_pack_builder.md` / `cocoro_ghost/internal_worker.py`）。
- **RetrievalにLLMを使わない**: Query Expansion / LLM Rerank は採用せず、速度を優先する（`docs/retrieval.md`）。

## ドキュメント一覧（読む順番）

1. `docs/architecture.md`（全体像・データフロー）
2. `docs/settings_db.md`（settings.db / token / presets）
3. `docs/db_schema.md`（DDL / Enum / 永続化ルール）
4. `docs/sqlite_vec.md`（vec0設計・KNN→JOIN）
5. `docs/memory_pack_builder.md`（MemoryPack編成・スコア・圧縮）
6. `docs/retrieval.md`（記憶検索: LLMレス高速化版）
7. `docs/partner_mood.md`（パートナーの反射/機嫌: 即時反応 + 持続）
8. `docs/worker.md`（ジョブ・冪等性・版管理）
9. `docs/prompts.md`（LLM JSONスキーマ）
10. `docs/prompt_usage_map.md`（プロンプト使用箇所マップ）
11. `docs/api.md`（API仕様 / SSE）
12. `docs/bootstrap.md`（初期DB作成）

## 用語

- **Unit**: 記憶DB（`memory_<embedding_preset_id>.db`）で扱う「1つの記憶/出来事/生成物」の基本単位。共通メタを `units` に1行で持ち、`kind/state/sensitivity/pin/topic_tags` 等で扱いを決める（詳細は `docs/db_schema.md`）。
- **Payload**: Unitの本文や構造化データを、種別ごとにスキーマ分離したテーブル群（`payload_episode` / `payload_fact` / `payload_summary` / `payload_loop` / `payload_capsule` など）。Unitと同じ `unit_id` で1:1に紐づく（詳細は `docs/db_schema.md`）。
- **UnitKind / UnitState / Sensitivity**: Unitの「種別」「状態」「取り扱い区分」をenum値で表すもの。検索・注入・Worker処理の対象範囲を決めるための土台（詳細は `docs/db_schema.md` と実装の `cocoro_ghost/unit_enums.py`）。
- **Canonical / Derived**: “原文（証跡）” と “派生物” を分ける考え方。
  - Canonical: ユーザー発話や通知本文など「改変しないログ」（例: `EPISODE`）。
  - Derived: Workerで抽出/統合された「解釈・要約・構造化」（例: `FACT` / `SUMMARY` / `LOOP` / `CAPSULE`）。
- **MemoryPack**: `/api/chat` の同期処理中に、MemoryPack Builderが「LLMへ注入するため」に組み立てるテキストパック。見出し順（`<<<COCORO_GHOST_SECTION:CONTEXT_CAPSULE>>>` 等）に沿って、検索結果をそのまま貼らずに圧縮・整形する（仕様: `docs/memory_pack_builder.md`、実装: `cocoro_ghost/memory_pack_builder.py`）。
- **Retriever**: 記憶検索システム。固定クエリ → Hybrid Search (Vector + BM25) → ヒューリスティック Rerank の3段階で、会話に関連する過去のエピソードを高速に選別する（仕様: `docs/retrieval.md`）。LLMレスで高速に動作。
- **Persona / Addon**: LLM注入プロンプトを「人格」と「任意追加オプション」に分けたもの。
  - Persona: 人格・口調・価値観の中核（崩れると会話の一貫性が壊れる）。
  - Addon: 必要なときだけ足す補助指示（例: 表情タグの追加ルール、呼称の追加、距離感の微調整）。
  - 注入上は、Persona/Addon を system prompt に固定注入し、MemoryPackは `<<INTERNAL_CONTEXT>>` の内部メッセージとして渡す（`docs/memory_pack_builder.md` / `docs/api.md`）。
- **Preset（settings）**: `settings.db` に永続化する切替単位。
  - LLM/Embeddingの接続情報・検索予算に加え、Persona/Addon をプリセットとして保持し、`active_*_preset_id` でアクティブを選ぶ（`docs/settings_db.md`）。
- **embedding_preset_id**: 記憶DBファイル名を選ぶための識別子。`EmbeddingPreset.id`（UUID）を `embedding_preset_id` として扱い、`memory_<embedding_preset_id>.db` を開く（`docs/settings_db.md` / `docs/api.md`）。


## LLMの構造化出力について（採用しない理由）

※忘れないためのメモ（設計判断）。

本プロジェクトでは「本文（ユーザー表示） + 内部メタ（反射/機嫌など）」を **1回のLLM呼び出し**で取得しつつ、
`/api/chat` の **SSEストリーミング（本文の逐次送信）** を維持したい。
この前提のもとで、以下理由より Structured Outputs / tool call を採用するのは現状見送っている。

- tool call（function calling）
  - `LiteLLM + Gemini` の組み合わせで1回目で呼ばれる呼ばれないケースがある。（ほぼ呼ばれない）
  - 複雑なJSONが扱えない。
- Structured Outputs
  - 出力全体がJSONになるため、本文を自然文のままストリームし続けるには「ストリーム中のJSONを壊さずに部分抽出する専用パーサ」が必要になる
  - 本文末尾につけてJSONが壊れないのであればメリットがない。

なお、履歴のノイズ化・入力トークン増・プロンプトキャッシュの当たり悪化を避けるため、内部JSONは次ターンの会話履歴に入れない。

具体仕様は `docs/prompts.md` の「chat（SSE）: 返答末尾の内部JSON（partner_affect trailer）」を参照
