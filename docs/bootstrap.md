# 初期化（運用前提：新規DB）

本プロジェクトは「運用中データの移行」を前提にしないため、**新仕様のスキーマでDBを新規初期化**します。

## 手順

1. `settings.db` と `memory_<memory_id>.db` を初期化する
   - DDL: `docs/db_schema.md`
   - vec0: `docs/sqlite_vec.md`
2. 最低限の seed を入れる（任意だが推奨）
   - `payload_persona`（人格コア）を 1件（active）
   - `payload_contract`（関係契約）を 1件（active）

## seed例（SQL）

`memory_<memory_id>.db` に対して実行する。

> 実装では、`payload_persona` / `payload_contract` が 1件も無い場合に限り、起動時に default を自動seedする（`cocoro_ghost/db.py`）。

```sql
-- Persona
insert into units(kind, occurred_at, created_at, updated_at, source, state, confidence, salience, sensitivity, pin)
values (4, strftime('%s','now'), strftime('%s','now'), strftime('%s','now'), 'seed', 0, 0.5, 0.0, 0, 1);
insert into payload_persona(unit_id, persona_text, is_active)
values (last_insert_rowid(), '（ここに人格コアを書く）', 1);

-- Contract
insert into units(kind, occurred_at, created_at, updated_at, source, state, confidence, salience, sensitivity, pin)
values (5, strftime('%s','now'), strftime('%s','now'), strftime('%s','now'), 'seed', 0, 0.5, 0.0, 0, 1);
insert into payload_contract(unit_id, contract_text, is_active)
values (last_insert_rowid(), '（ここに関係契約を書く）', 1);
```
