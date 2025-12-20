# Worker（非同期ジョブ）仕様

## 目的

- APIプロセスのレイテンシを守るため、重い処理を非同期化する
- Derivedメモリ（facts/summaries/loops/entities/embeddings）を増やし、長期の一貫性を強化する

## Jobテーブル

`jobs` テーブルは `memory_<memory_id>.db` に永続化する（DDLは `docs/db_schema.md`）。

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
- `weekly_summary(week_key)`（定期 / `memory_id` は Worker が扱うDBで暗黙）
- `person_summary_refresh(entity_id)`（人物サマリ更新）
- `topic_summary_refresh(entity_id)`（トピックサマリ更新）
- `capsule_refresh(limit)`（任意 / `limit` は直近件数、デフォルト5）

## 実装ステータス（Current/Planned）

- Current: weekly_summary は Episode保存後に必要なら自動enqueue（重複抑制・クールダウンあり）+ 管理APIからもenqueue（定期スケジュールは未実装）。
- Current: person/topic summary は `extract_entities` 後に重要度上位（最大3件ずつ）を自動enqueue（重複抑制あり）。
- Current: capsule_refresh は Episode保存後の既定ジョブとして自動enqueue（`limit=5`）。
- Planned: 定期実行（cron）で relationship/person/topic の summary を refresh（差分更新/対象選定の改善）。

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

## 複数 memory_id の運用

- Worker は **`memory_<memory_id>.db` ごとに 1プロセス**で動かす（1DB=1ジョブキュー）
- 複数 `memory_id` を運用する場合は、`memory_id` ごとに Worker を起動する
  - 通常 `memory_id` は `settings.db` の `embedding_presets.id`（UUID）で、`active_embedding_preset_id` が既定で使われる
  - 例: `python -X utf8 run_worker.py --memory-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

## topic_tags の保存（推奨）

- `units.topic_tags` は **JSON array文字列**で保存する（CSVは使わない）
- 保存前に NFKC 正規化 + trim + 重複除去 + ソートを行い、`payload_hash` が安定するようにする

## Weekly Summary の保存（推奨）

- `payload_summary.summary_text` は注入用のプレーンテキスト
- `payload_summary.summary_json` に LLM 出力JSON（`summary_text/key_events/relationship_state`）を丸ごと保存する

## Workerループ（実装指針）

1. `jobs.status=0` かつ `run_after <= now` を `ORDER BY run_after, id` で取得
2. 取得したジョブを `status=1` にしてロック（単一プロセスならトランザクションで十分）
3. 実行
4. 成功: `status=2`, `updated_at=now`
5. 失敗: `tries += 1`, `last_error` 記録、`status=0` に戻して `run_after` を指数バックオフ、一定回数で `status=3`
