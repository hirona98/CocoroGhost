# 初期化/マイグレーション方針

## 運用していない場合（推奨：マイグレーション不要）

既存ユーザーの継続データが不要なら、段階的移行は不要です。**新仕様のスキーマで新規にDBを初期化**してください。

### 推奨手順

1. `settings.db` と `memory_<memory_id>.db` を新仕様で初期化する
   - DDL: `docs/partner_spec/db_schema.md`
   - vec0: `docs/partner_spec/sqlite_vec.md`
2. 最低限の seed を入れる（任意だが推奨）
   - `payload_persona`（人格コア）を 1件（active）
   - `payload_contract`（関係契約）を 1件（active）
3. 実装を `units/payload_*` 前提で進める（旧 `episodes/persons` は不要）

## 既存データを取り込みたい場合（参考）

もし過去DB（`episodes/persons/episode_vectors` 等）から取り込みたい場合のみ、段階的移行を行う。

- 旧DBを残したまま新テーブルを追加（並走）
- `episodes → units(kind=EPISODE)+payload_episode`
- `persons → entities/entity_aliases`
- `episode_persons → unit_entities`
- `episode_vectors → vec_units`

