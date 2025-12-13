# DB設計（Unit化 + Payload分離）

## 設計思想（必須）

- **“Unit（共通メタ）＋ Payload（種別ごとの内容）”** に統一する
- すべての記憶は `units` に 1行の共通メタを持つ
- 本文は `payload_*` に分離（種別で固定スキーマ化）
- ベクター索引は `vec_units`（sqlite-vec）に分離
  - `unit_id` で JOIN して本文を取得（sqlite-vec推奨パターン）

## 時刻と型（必須）

- 時刻は **UTC epoch seconds（INTEGER）** を推奨
  - SQLiteの比較・index最適化のため
- JSON文字列は `TEXT` で保持（将来 `json_extract` 等で参照する前提）

## DDL（memory_<memory_id>.db）

### `units`（共通メタ）

```sql
create table if not exists units (
  id            integer primary key,
  kind          integer not null,        -- enum: UnitKind
  occurred_at   integer,                 -- UTC epoch sec (出来事時刻)
  created_at    integer not null,
  updated_at    integer not null,

  source        text,                    -- chat/desktop/camera/notification/meta_request/...
  state         integer not null default 0,   -- UnitState
  confidence    real    not null default 0.5, -- 0..1
  salience      real    not null default 0.0, -- 0..1
  sensitivity   integer not null default 0,   -- Sensitivity
  pin           integer not null default 0,   -- 0/1

  -- 任意：検索補助
  topic_tags    text,                    -- "仕事,読書" etc（CSV or JSON）
  emotion_label text,                    -- joy/sadness/anger/fear/neutral
  emotion_intensity real                 -- 0..1
);

create index if not exists idx_units_kind_created on units(kind, created_at);
create index if not exists idx_units_occurred on units(occurred_at);
create index if not exists idx_units_state on units(state);
```

### Entity / Link（軽量グラフ：Neo4j不要）

```sql
create table if not exists entities (
  id           integer primary key,
  etype        integer not null, -- EntityType
  name         text not null,
  normalized   text,
  created_at   integer not null,
  updated_at   integer not null
);
create index if not exists idx_entities_type_name on entities(etype, name);

create table if not exists entity_aliases (
  entity_id    integer not null references entities(id) on delete cascade,
  alias        text not null,
  primary key(entity_id, alias)
);
create index if not exists idx_entity_aliases_alias on entity_aliases(alias);

create table if not exists unit_entities (
  unit_id      integer not null references units(id) on delete cascade,
  entity_id    integer not null references entities(id) on delete cascade,
  role         integer not null,      -- EntityRole
  weight       real    not null default 1.0,
  primary key(unit_id, entity_id, role)
);
create index if not exists idx_unit_entities_entity on unit_entities(entity_id);

create table if not exists edges (
  src_entity_id    integer not null references entities(id) on delete cascade,
  rel_type         integer not null,  -- RelationType
  dst_entity_id    integer not null references entities(id) on delete cascade,
  weight           real    not null default 1.0,
  first_seen_at    integer,
  last_seen_at     integer,
  evidence_unit_id integer references units(id),
  primary key(src_entity_id, rel_type, dst_entity_id)
);
create index if not exists idx_edges_src on edges(src_entity_id);
create index if not exists idx_edges_dst on edges(dst_entity_id);
```

### 版管理（上書き禁止の差分運用）

```sql
create table if not exists unit_versions (
  unit_id        integer not null references units(id) on delete cascade,
  version        integer not null,
  parent_version integer,
  patch_reason   text,
  payload_hash   text,
  created_at     integer not null,
  primary key(unit_id, version)
);
```

## Payloadテーブル（パートナー最適）

### Episode（証跡：出来事/会話ログ）

```sql
create table if not exists payload_episode (
  unit_id         integer primary key references units(id) on delete cascade,
  user_text       text,
  reply_text      text,
  image_summary   text,
  context_note    text,
  reflection_json text
);
```

### Fact（安定知識：証拠リンク必須）

```sql
create table if not exists payload_fact (
  unit_id               integer primary key references units(id) on delete cascade,
  subject_entity_id     integer references entities(id),
  predicate             text not null,       -- "likes", "prefers", "is", ...
  object_text           text,
  object_entity_id      integer references entities(id),
  valid_from            integer,
  valid_to              integer,
  evidence_unit_ids_json text not null       -- JSON array of unit_id
);

create index if not exists idx_fact_subject_pred on payload_fact(subject_entity_id, predicate);
```

### Summary / SharedNarrative（要約）

```sql
create table if not exists payload_summary (
  unit_id      integer primary key references units(id) on delete cascade,
  scope_type   integer not null,    -- SummaryScopeType
  scope_key    text not null,       -- "2025-W50", "person:123", "topic:unity" ...
  range_start  integer,
  range_end    integer,
  summary_text text not null
);

create index if not exists idx_summary_scope on payload_summary(scope_type, scope_key);
```

### PersonaAnchor（人格コア：常時注入）

```sql
create table if not exists payload_persona (
  unit_id      integer primary key references units(id) on delete cascade,
  persona_text text not null,
  is_active    integer not null default 1
);
```

### RelationshipContract（踏み込み/NG/介入許可：常時注入）

```sql
create table if not exists payload_contract (
  unit_id       integer primary key references units(id) on delete cascade,
  contract_text text not null,
  is_active     integer not null default 1
);
```

### Capsule（短期状態：会話テンポのため）

```sql
create table if not exists payload_capsule (
  unit_id      integer primary key references units(id) on delete cascade,
  expires_at   integer,
  capsule_json text not null
);
```

### OpenLoop（未完了：次に話す理由）

```sql
create table if not exists payload_loop (
  unit_id    integer primary key references units(id) on delete cascade,
  status     integer not null, -- 0 open / 1 closed
  due_at     integer,
  loop_text  text not null
);

create index if not exists idx_loop_status_due on payload_loop(status, due_at);
```

## Jobテーブル（Worker用：SQLiteで永続化）

```sql
create table if not exists jobs (
  id          integer primary key,
  kind        text not null,           -- "reflect", "extract", "embed", "summarize", ...
  payload_json text not null,
  status      integer not null default 0, -- 0 queued / 1 running / 2 done / 3 failed
  run_after   integer not null,
  tries       integer not null default 0,
  last_error  text,
  created_at  integer not null,
  updated_at  integer not null
);

create index if not exists idx_jobs_status_run_after on jobs(status, run_after);
```

## Enum定義（実装で固定すること）

### UnitKind

| name | value | 用途 |
|---|---:|---|
| EPISODE | 1 | 証跡（会話/出来事） |
| FACT | 2 | 安定知識（好み/関係/設定） |
| SUMMARY | 3 | 要約（週次/人物別/トピック別/共有ナラティブ） |
| PERSONA | 4 | 人格コア |
| CONTRACT | 5 | 関係契約 |
| CAPSULE | 6 | 短期状態 |
| LOOP | 7 | 未完了（open loops） |

### UnitState

| name | value | 意味 |
|---|---:|---|
| RAW | 0 | 生成直後 |
| VALIDATED | 1 | 抽出/整合の一次検証済み |
| CONSOLIDATED | 2 | 統合済み（重複削減・要約化） |
| ARCHIVED | 3 | アーカイブ（通常検索から除外） |

### Sensitivity

| name | value | 意味 |
|---|---:|---|
| NORMAL | 0 | 通常 |
| PRIVATE | 1 | UI/外部連携で取り扱い注意 |
| SECRET | 2 | 原則注入しない（明示要求時のみ） |

### EntityType（例）

| name | value |
|---|---:|
| PERSON | 1 |
| PLACE | 2 |
| PROJECT | 3 |
| WORK | 4 |
| TOPIC | 5 |
| ORG | 6 |

### SummaryScopeType（例）

| name | value | scope_key例 |
|---|---:|---|
| DAILY | 1 | `2025-12-13` |
| WEEKLY | 2 | `2025-W50` |
| PERSON | 3 | `person:123` |
| TOPIC | 4 | `topic:unity` |
| RELATIONSHIP | 5 | `relationship:user` |

## SQLite 推奨PRAGMA（起動時）

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA foreign_keys=ON;
```

