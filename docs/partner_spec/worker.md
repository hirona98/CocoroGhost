# Worker（非同期ジョブ）仕様

## 目的

- APIプロセスのレイテンシを守るため、重い処理を非同期化する
- Derivedメモリ（facts/summaries/loops/entities/embeddings）を増やし、長期の一貫性を強化する

## Jobテーブル（必須）

`jobs` テーブルは `memory_<memory_id>.db` に永続化する（DDLは `docs/partner_spec/db_schema.md`）。

### status

- 0 queued
- 1 running
- 2 done
- 3 failed

## ジョブ種別（必須）

- `reflect_episode(unit_id)`
- `extract_entities(unit_id)`
- `extract_facts(unit_id)`
- `extract_loops(unit_id)`
- `upsert_embeddings(unit_id)`（episode/fact/summary/loop…必要種別）
- `weekly_summary(week_key)`（定期 / `memory_id` は Worker が扱うDBで暗黙）
- `capsule_refresh(limit)`（任意 / `limit` は直近件数、デフォルト5）

## 冪等性ルール（必須）

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

## Workerループ（実装指針）

1. `jobs.status=0` かつ `run_after <= now` を `ORDER BY run_after, id` で取得
2. 取得したジョブを `status=1` にしてロック（単一プロセスならトランザクションで十分）
3. 実行
4. 成功: `status=2`, `updated_at=now`
5. 失敗: `tries += 1`, `last_error` 記録、`status=0` に戻して `run_after` を指数バックオフ、一定回数で `status=3`
