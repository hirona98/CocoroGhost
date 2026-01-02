# Worker（非同期ジョブ）仕様

## 目的

- APIプロセスのレイテンシを守るため、重い処理を非同期化する
- Derivedメモリ（facts/summaries/loops/entities/embeddings）を増やし、長期の一貫性を強化する

## Jobテーブル

`jobs` テーブルは `memory_<embedding_preset_id>.db` に永続化する（DDLは `docs/db_schema.md`）。

### status

- 0 queued
- 1 running
- 2 done
- 3 failed

## ジョブ種別

- `reflect_episode(unit_id)`
- `extract_entities(unit_id)`
- `extract_facts(unit_id)`
- `extract_loops(unit_id)`
- `upsert_embeddings(unit_id)`（episode/fact/summary/loop…必要種別）
- `bond_summary(scope_key=rolling:7d)`（定期 / `embedding_preset_id` は Worker が扱うDBで暗黙）
- `person_summary_refresh(entity_id)`（人物サマリ更新）
- `topic_summary_refresh(entity_id)`（トピックサマリ更新）
- `capsule_refresh(limit)`（任意 / `limit` は直近件数、既定5）

補足:
- `/api/chat` は「返答本文 + 内部JSON（反射）」を同一LLM呼び出しで得て、Episodeへ即時反映するため、`reflect_episode` は既に反射が入っている場合は冪等にスキップされます（フォールバック用として残す）。

## 実装ステータス（Current/Planned）

- Current: bond summary（rolling:7d）は Episode保存後に必要なら自動enqueue（重複抑制・クールダウンあり）。
- Current: person/topic summary は `extract_entities` 後に重要度上位（最大3件ずつ）を自動enqueue（重複抑制あり）。
- Current: capsule_refresh は Episode保存後の既定ジョブとして自動enqueue（`limit=5`）。
- Current: cron無し運用のため、Worker内で定期enqueue（weekly/person/topic/capsule）も実施できる（固定値: 30秒ごとに判定）。
- Current: 起動コマンドは `run.py` のみ（内蔵Workerがバックグラウンドで動作）。
- Current: `/api/settings` で active preset / embedding_preset_id を切り替えると、内蔵Workerは自動で再起動して追従する。
- Non-goal: uvicorn multi-worker 等の多重起動は未対応（内蔵Workerが重複実行されうるため）。`workers=1` 前提で運用する。

## 冪等性ルール

- 同じ `unit_id` に対する同種ジョブは **何度実行しても整合が保てる**こと
- Upsertは `unit_id` をキーにする
- `unit_versions` を更新して来歴を残す（payload hash で変更検出）

## 版管理（unit_versions）

### 方針

- payload の「上書き」はしない（履歴が必要）
- 新しい派生結果が得られた場合は
  - `unit_versions` に `version=previous+1` を追加
  - `payload_hash` を更新して変更検出できるようにする

### payload_hash

- JSONを正規化（キー順ソート等）した文字列のhash（例: SHA-256）を推奨
- 同一hashなら再実行しても「更新なし」として扱う

## 複数 embedding_preset_id の運用

- 内蔵Workerは **アクティブな `embedding_preset_id`（= `active_embedding_preset_id`）1つ**を処理対象にする
- 別 `embedding_preset_id` を扱いたい場合は `/api/settings` で切り替える（切替後、内蔵Workerが自動再起動して追従する）

## 定期実行（cron無し）

cron が無い環境向けに、Worker（jobs処理ループ）内で定期的に jobs を enqueue する。

- enqueue 対象: `bond_summary` / `person_summary_refresh` / `topic_summary_refresh` / `capsule_refresh`
- 重複抑制: queued/running の同種ジョブがあれば enqueue しない
- クールダウン: summary/capsule の更新頻度を抑制（デフォルト: 30秒ごとに判定）

起動例:

```bash
python -X utf8 run.py
```

補足:
- 既定では `run.py` の起動時に **内蔵Worker（バックグラウンド）** が開始される（起動コマンド1本）。

## topic_tags の保存（推奨）

- `units.topic_tags` は **JSON array文字列**で保存する（CSVは使わない）
- 保存前に NFKC 正規化 + trim + 重複除去 + ソートを行い、`payload_hash` が安定するようにする

## Weekly Summary の保存（推奨）

- `payload_summary.summary_text` は注入用のプレーンテキスト
- `payload_summary.summary_json` に LLM 出力JSON（`summary_text/key_events/bond_state`）を丸ごと保存する

## Workerループ（実装指針）

1. `jobs.status=0` かつ `run_after <= now` を `ORDER BY run_after, id` で取得
2. 取得したジョブを `status=1` にしてロック（単一プロセスならトランザクションで十分）
3. 実行
4. 成功: `status=2`, `updated_at=now`
5. 失敗: `tries += 1`, `last_error` 記録、`status=0` に戻して `run_after` を指数バックオフ、一定回数で `status=3`
