# 参考実装メモ: AutoMem（`Reference/automem`）

作成日: 2026-01-25

参照元（ローカル）:

- `D:\AliceEncoder\DesktopAssistant\Reference\automem`（WSL上では `/mnt/d/AliceEncoder/DesktopAssistant/Reference/automem`）

このドキュメントは、AutoMem（外部メモリ実装）の設計・実装から、CocoroGhostの「AI人格×記憶」設計に流用できそうな箇所を拾って整理するメモです。

## AutoMemの要約（何をしているか）

- グラフDB（FalkorDB）とベクトルDB（Qdrant）を併用し、記憶の保存・検索・関連付け・育成を行う。
- コアは大きく3つ:
  1) **enrichment**（記憶にタグ/エンティティ/関係/要約などを付与して“構造化”）
  2) **recall**（ハイブリッドスコアリング＋グラフ展開で“多段の想起”）
  3) **consolidation**（減衰/クラスタリング/メタ記憶化などで“整理/圧縮”）

## CocoroGhostで参考にできそうな場所（コード単位）

### 1) Enrichment（エンティティタグ化＋派生関係の生成）

該当実装:

- `app.py`（AutoMem本体）
  - `extract_entities()`（spaCy+正規表現で entities 抽出）
  - `enrich_memory()`（タグ付け、entityタグ生成、tag_prefixes生成、summary生成、関係生成）
  - `find_temporal_relationships()`（`PRECEDED_BY` 的な時系列リンク）
  - `link_semantic_neighbors()`（近傍ベクトルから `SIMILAR_TO` 辺を張る）
  - `detect_patterns()`（同タイプの記憶群から Pattern ノード生成＋ `EXEMPLIFIES`）
- `automem/utils/tags.py`（タグprefixの事前計算）

CocoroGhostへの示唆:

- 既に `WritePlan.event_annotations.entities` を持っているので、**entities を “検索用インデックス” に落とす**のが次の一手になり得る。
  - 例: `entity:people:<slug>` のような正規化キーを作り、`state.payload_json` や専用テーブルに持たせる。
  - これがあると「種（seed）→entity展開→関連事実」の多段想起が作りやすい。
- `assistant_summary`（選別入力の軽量化）と同様に、**短いsummaryを派生情報として常備**する設計は相性が良い（AutoMemは `summary` を node property として持つ）。
- `find_temporal_relationships()` は、CocoroGhostの `reply_to` や `event_threads` と同種の「流れ」を補強する考え方として参考。

### 2) Recall（ハイブリッドスコアリング）

該当実装:

- `automem/utils/scoring.py`
  - vector/keyword/relation/tag/importance/confidence/recency/exact + context bonus を重み付けして最終スコア化

CocoroGhostへの示唆:

- CocoroGhostは「候補収集を広め→LLM選別」を採用しているが、選別前の候補の並びやクォータ配分を安定させるために、
  - **“候補に付けるスコア成分”を明示してログに残す**
  - **重要/信頼/最近性/完全一致**のような信号で“席取り”を安定させる
 という方向は有効。
- 特に `confidence` をスコアに組み込む発想は、CocoroGhostの `WritePlan.*_confidence` を検索ランクに反映するヒントになる。

### 3) Graph expansion（関係展開＋entity展開の多段想起）

該当実装:

- `automem/api/recall.py`
  - `_expand_related_memories()`（seedからグラフ辺を辿って候補を追加、strength/importance閾値でノイズ抑制）
  - `_expand_entity_memories()`（seedに含まれるentitiesから entityタグ検索して候補を追加）
- `docs/API.md`（`expand_relations` / `expand_entities` / `expand_min_*` の公開仕様）

CocoroGhostへの示唆:

- CocoroGhost側で近い形にするなら、
  - `SearchResultPack` 選別前の候補収集ステップに **“展開枠”** を追加
  - 展開元（seed）の根拠（hit_sources）を保持
  - 展開候補は **importance/strength/confidence で足切り**（ノイズ抑制）
 という形が分かりやすい。
- “多段の連想”はLLM任せにするとブレるので、AutoMemのように **展開のルールを固定**しておき、最後をLLM選別に任せるのはCocoroGhostの方針とも一致する。

### 4) Consolidation（減衰・クラスタ・メタ記憶）

該当実装:

- `consolidation.py`
  - `calculate_relevance_score()`（指数減衰＋アクセス＋関係密度＋importance/confidence）
  - `cluster_similar_memories()`（近似クラスタを作り “MetaMemory” を生成して `SUMMARIZES` 辺を張る）
  - forgetting/archive/delete（CocoroGhostの設計方針とは衝突し得る）

CocoroGhostへの示唆:

- CocoroGhostは「削除しない」が原則なので、AutoMemの “forget/delete” はそのままは採用しにくい。
- ただし **「メタ記憶（まとめノード）を作ってリンクする」** は、`state.kind="summary"` を“育てる”方向性として相性が良い。
  - 例: thread/topic単位の summary state を作り、元イベント（または状態）への参照リンクを保持する。

## まとめ（CocoroGhost観点で“刺さる”順）

1) **entityタグ/インデックス + entity展開**（多段想起を安定化できる）
2) **展開候補のノイズ抑制（strength/importance/confidence閾値）**（LLM選別の入力品質を上げる）
3) **メタ記憶（summaryノード）の生成とリンク**（stateの整理と検索効率に効く）

