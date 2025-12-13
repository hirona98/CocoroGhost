# 実装計画（現行実装 → パートナー最適仕様）

## 1. 変更の要点（差分サマリ）

### 現行（レガシー）

- 記憶: `episodes/persons/episode_persons` + sqlite-vec `episode_vectors(rowid=episodes.id)`
- /chat: 類似エピソードをそのまま注入しがち（systemへ貼る）
- Reflection/embedding: /chat 後のBackgroundTaskで更新

### 新仕様（ターゲット）

- 記憶: **Unit化** `units` + 種別ごとの `payload_*`
- ベクター: **`vec_units(unit_id, embedding, kind, metadata...)`**（本文はJOINで取得）
- 会話注入: **SchedulerがMemoryPackを編成**（persona/contract/facts/summaries/loops/episodes）
- 派生処理: **Workerが jobs から非同期**（reflection/entities/facts/loops/summary/embed）
- 運用前提: sqlite-vecはpre-v1なので **バージョン固定**

## 2. マイルストーン（運用前提：移行なし）

### M0: ドキュメントとEnum固定

- `UnitKind/UnitState/Sensitivity/...` を実装側のEnumとして固定し、DBと一致させる
- 本ディレクトリの仕様を「正」として合意する（途中で値を変えない）

### M1: 新仕様DBを初期化

- `memory_<id>.db` を新仕様で初期化する（旧 `episodes/persons` は不要）
  - `units`, `payload_*`, `entities`, `edges`, `unit_versions`, `jobs`, `vec_units`
- sqlite-vec拡張ロードと `vec_units` 作成を初期化に組み込む
- PRAGMA（WAL等）を起動時に適用する

### M2: `/chat` を units 前提で実装

- 保存は `units(kind=EPISODE)+payload_episode` を RAW で行う
- `jobs` に `reflect/extract/embed` 等を enqueue する（同期は軽く保つ）

### M3: Worker導入（派生の増殖）

- Workerプロセス（または別スレッド）で `jobs` を実行して派生を書き込む
- 最低限: reflection / embedding upsert
- 次に: entities → facts → loops の順に増やす

### M4: Scheduler導入（MemoryPack）

- 常時注入（persona/contract）から開始し、facts/summaries/loops/knn を段階的に追加
- intent分類は最終的に `docs/partner_spec/prompts.md` のJSON出力へ

### M5: 週次サマリ等のLifecycle

- Weekly SharedNarrative生成ジョブ（cron相当）
- Capsule（短期状態）保存/更新（任意）

### M6: 管理API（任意）

- units/facts/loops/summaries の閲覧・pin/sensitivity/state更新

## 3. タスク分解（P0/P1/P2）

### P0（必須）

- DDL実装（`docs/partner_spec/db_schema.md`）
- sqlite-vec `vec_units` 実装（v0.1.6+前提・ロード手順）
- `/chat` 同期フロー移植
  - Scheduler → MemoryPack → LLM注入 → SSE配信 → Episode保存（RAW）→ Job enqueue
- Worker P0
  - `reflect_episode`, `extract_entities`, `extract_facts`, `extract_loops`, `upsert_embeddings`
- 設定DB読み込み（active preset / token / memory_id / max_inject_tokens 等）

### P1（最適化の核）

- Weekly SharedNarrative生成ジョブ（cron相当）
- Capsule（短期状態）保存/更新（任意だが体感向上）
- `unit_versions` + `payload_hash` による変更検出（冪等性の基盤）

### P2（管理・信頼）

- 管理API（read-onlyでも可）：units/facts/loops/summaries の閲覧
- ピン留め・sensitivity変更（UIは後回しでもAPIだけ先行）

## 4. 既存コードへの当て込み（目安）

現行の責務と新仕様の責務が大きく異なるため、運用前提なら **置換（新仕様へ一括移行）** がシンプル。  
既存データ取り込みや互換が必要な場合のみ、並走→切替（段階的移行）を検討する。

- `cocoro_ghost/models.py`
  - 旧: `Episode/Person/EpisodePerson`
  - 新: `Unit` と `payload_*`、`Entity*`、`Job` を追加（またはファイル分割）
- `cocoro_ghost/db.py`
  - 旧: `episode_vectors` の作成・検索
  - 新: `vec_units` の作成・upsert・KNN、PRAGMA適用、（将来的に）マイグレーション補助
- `cocoro_ghost/memory.py`
  - 旧: /chat の同期+類似検索+反省の一部
  - 新: /chat 同期は「保存（RAW）+ enqueue」まで。派生はWorkerへ委譲
- 新規追加（推奨）
  - `cocoro_ghost/scheduler.py`（MemoryPack編成）
  - `cocoro_ghost/worker.py`（jobs実行）
  - `cocoro_ghost/store.py`（Unit/payloadの保存・読み取りAPI）

## 5. 互換/移行の考え方（必要時のみ）

- 既存データを取り込みたい場合のみ `docs/partner_spec/migration.md` を参照
- クライアントAPI互換が必要なら
  - 旧 `user_id/text/image_base64` と、新 `memory_id/user_text/images[]` の両対応期間を作る
  - SSEは `type` 埋め込み方式→ `event` 名方式へ段階的に移行（両方送る等）

## 6. リスクとチェックリスト

- embedding次元の変更は vec0 に直撃する（再構築/別DBが必要）
- sqlite-vecのバージョン固定（依存関係のピン留め）が必須
- `unit_versions` を入れないと冪等性が崩れやすい（再実行で増殖/上書き事故）
