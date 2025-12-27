# 初期化

DBは `settings.db` と `memory_<memory_id>.db` を新規作成し、スキーマを初期化する。

## 手順

1. `settings.db` と `memory_<memory_id>.db` を初期化する
   - DDL: `docs/db_schema.md`
   - vec0: `docs/sqlite_vec.md`
2. 必要なら最低限の seed を入れる
   - `persona_presets`（人格コア）を 1件
   - `contract_presets`（addon: 任意追加オプション）を 1件
   - `global_settings.active_*_preset_id` を上記に紐付ける

## seed例（SQL）

`settings.db` に対して実行する。

> 実装では、`settings.db` が空の場合に限り、起動時に default を自動seedする（`cocoro_ghost/db.py` の `ensure_initial_settings`）。
>
> なお、本プロジェクトはプリセットの主キーに UUID（TEXT）を使用するため、SQLで直接seedする場合は `id` を明示的に指定する（UUIDを事前生成する）必要がある。

```sql
-- Persona
insert into persona_presets(id, name, persona_text, created_at, updated_at)
values ('xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', 'default', '（ここに人格コアを書く）', datetime('now'), datetime('now'));

-- Addon（互換のためテーブル/カラム名は contract_* のまま）
insert into contract_presets(id, name, contract_text, created_at, updated_at)
values ('xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', 'default', '（ここに追加オプションを書く）', datetime('now'), datetime('now'));

-- global_settings の active_* を更新
update global_settings
set
  active_persona_preset_id = (select id from persona_presets where name='default'),
  active_contract_preset_id = (select id from contract_presets where name='default');
```
