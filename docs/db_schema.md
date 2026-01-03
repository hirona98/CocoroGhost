# DB設計（Unit化 + Payload分離）

## 設計思想

このDBは「長期運用で壊れにくい」「検索が速い」「非同期処理で育つ」を優先し、次のルールで設計する。

### 1) Unit（共通メタ）＋ Payload（本文/構造）

- **すべてのデータは Unit として `units` に1行の共通メタを持つ**
  - 検索/注入/編集/版管理などの“共通操作”を `units` だけで完結できるようにするため。
- **本文・構造化データは `payload_*` に分離（種別ごとにスキーマを固定）**
  - EPISODE/FACT/SUMMARY/LOOP など、内容の形が違うものを1テーブルに詰めない（nullable地獄を避ける）。
  - 種別ごとに「どの列が正で、どの列が派生か」を明確にする（後段のWorkerの冪等性にも効く）。

### 2) Canonical（証跡）と Derived（派生）を分ける

- **EPISODE は証跡（Canonical）**：ユーザー発話・通知本文など「改変しないログ」。
- **FACT/SUMMARY/LOOP は派生（Derived）**：Workerが抽出/統合して生成する“解釈・整理”。
- ねらい:
  - 「派生が間違っても、証跡から再生成できる」＝長期運用での修復性。
  - 「同期(`/api/chat`)は軽く、重い生成はWorkerへ」＝会話テンポの安定化。

### 3) 検索用の索引は本文と分離する（JOIN前提）

- **Vector索引は `vec_units`（sqlite-vec/vec0）に分離**し、本文は持たない。
  - `unit_id` で `units` / `payload_*` に JOIN して本文を読む（sqlite-vec推奨パターン）。
  - ねらい: 索引の肥大化・更新コスト・スキーマ変更の影響範囲を局所化する。
- **BM25は `episode_fts`（FTS5 external content）** を使い、正の本文は `payload_episode`。
  - ねらい: “全文検索用の構造”と“正のデータ”を分けて、整合・再構築を扱いやすくする。

### 4) 版管理（unit_versions）で「更新履歴」を残す

- 派生物やメタ更新は上書きだけで終わらせず、**何がどう変わったか**を `unit_versions` に残す。
- ねらい:
  - LLM出力の揺れ・抽出ロジックの変更があっても、追跡・比較できる。
  - 再実行の冪等性（payload_hash一致なら更新不要）を作りやすい。

### 5) jobs は「非同期の契約」：冪等前提で積む

- Workerは `jobs` から処理を取り出して派生Unitを作る。
- 同じ入力で何度走っても整合が壊れない（upsert + 版管理）を前提にする。
- ねらい:
  - LLM失敗や一時的障害で止まっても、リトライで回復できる。
  - 同期処理の責務を最小化できる。

補足（重要）:
- `jobs` は **外部クライアント向けの汎用「ジョブ投入API」ではない**。
- 現状、ジョブは「APIプロセスが内部でenqueueする」か「管理APIで 背景共有サマリ（`rolling:7d`）のみ手動enqueueできる」だけ。
- つまりこの“契約”は **API（同期）⇄ 内蔵Worker（非同期）** の内部契約を指す。

### 6) state / sensitivity で「運用上のガード」を表現する

- `state` は「採用してよいか/統合済みか」の状態管理。
- `sensitivity` は「注入/表示/外部連携での取り扱い」を制御するためのガード。
- ねらい: “保存したもの全部を注入しない”ためのフィルタ軸をDB側に持つ。

## 時刻と型

- 時刻は **UTC epoch seconds（INTEGER）** を推奨
  - SQLiteの比較・index最適化のため
- JSON文字列は `TEXT` で保持（必要なら `json_extract` 等で参照する）

## DDL（memory_<embedding_preset_id>.db）

### `units`（共通メタ）

```sql
create table if not exists units (
  id            integer primary key,
  kind          integer not null,        -- enum: UnitKind
  occurred_at   integer,                 -- UTC epoch sec (出来事時刻)
  created_at    integer not null,
  updated_at    integer not null,

  source        text,                    -- chat/notification/meta-request/proactive/...
  state         integer not null default 0,   -- UnitState
  confidence    real    not null default 0.5, -- 0..1
  salience      real    not null default 0.0, -- 0..1
  sensitivity   integer not null default 0,   -- Sensitivity
  pin           integer not null default 0,   -- 0/1

  -- 任意：検索補助
  topic_tags    text,                    -- JSON array string（例: ["仕事","読書"]）
  persona_affect_label text,             -- joy/sadness/anger/fear/neutral
  persona_affect_intensity real          -- 0..1
);

create index if not exists idx_units_kind_created on units(kind, created_at);
create index if not exists idx_units_occurred on units(occurred_at);
create index if not exists idx_units_state on units(state);
```

#### カラム詳細（使い方）

| column | type | 意味/使い方 | 主な書き手 |
|---|---|---|---|
| `id` | INTEGER | Unitの主キー（自動採番）。他テーブルは `unit_id` で参照する。 | DB |
| `kind` | INTEGER | UnitKind（EPISODE/FACT/SUMMARY/LOOP）。Payloadテーブル選択のキー。 | API/Worker |
| `occurred_at` | INTEGER | 出来事の時刻（UTC epoch sec）。検索のrecencyや週次集計の基準になる。 | API/Worker |
| `created_at` | INTEGER | 作成時刻（UTC epoch sec）。 | API/Worker |
| `updated_at` | INTEGER | 更新時刻（UTC epoch sec）。派生物更新/編集時に更新。 | Worker/Admin |
| `source` | TEXT | 生成元（例: `chat`/`notification`/`meta-request`/`extract_facts` など）。監査・デバッグ用。 | API/Worker |
| `state` | INTEGER | UnitState。RAW→VALIDATED→CONSOLIDATED を想定。検索/注入で除外したいものは `ARCHIVED`。 | API/Worker/Admin |
| `confidence` | REAL | 内容の確からしさ（0..1）。Workerが抽出した推定値を入れる（反射/抽出）。 | Worker |
| `salience` | REAL | 注入優先度の指標（0..1）。Schedulerのfactスコア等に利用。 | Worker |
| `sensitivity` | INTEGER | Sensitivity。PRIVATE以上は外部UI/注入の制約に使う。 | API/Worker/Admin |
| `pin` | INTEGER | ピン留め（0/1）。Schedulerの採用優先度にボーナス。 | Admin |
| `topic_tags` | TEXT | JSON array文字列。NFKC正規化・重複除去・ソートで安定化推奨。 | Worker/Admin |
| `persona_affect_label` | TEXT | 反射（reflect_episode）の結果ラベル。 | Worker |
| `persona_affect_intensity` | REAL | 反射の強度（0..1）。 | Worker |

補足:
- `occurred_at` が無い場合は `created_at` を代替にしている箇所がある（Retrieverの時刻など）。
- `state`/`sensitivity` は vec0 側にもメタ列として同期される（検索フィルタ用）。

### Entity / Link（軽量グラフ）

```sql
	create table if not exists entities (
	  id           integer primary key,
	  type_label   text,             -- 自由ラベル（例: PERSON/TOPIC/ORG/...）。保存時は大文字に正規化。
	  name         text not null,
	  normalized   text,
	  roles_json   text not null,    -- JSON array（例: ["person"] / ["topic"]）。保存時は小文字に正規化。
	  created_at   integer not null,
	  updated_at   integer not null
	);
	create index if not exists idx_entities_label_name on entities(type_label, name);
	create index if not exists idx_entities_normalized on entities(normalized);

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
  relation_label   text    not null,  -- 自由ラベル（例: friend/likes/mentor/...）
  dst_entity_id    integer not null references entities(id) on delete cascade,
  weight           real    not null default 1.0,
  first_seen_at    integer,
  last_seen_at     integer,
  evidence_unit_id integer references units(id),
  primary key(src_entity_id, relation_label, dst_entity_id)
);
create index if not exists idx_edges_src on edges(src_entity_id);
create index if not exists idx_edges_dst on edges(dst_entity_id);
```

#### 使い方（Entity解決）

- `entities` は「人物/トピックなどの正規エンティティ」。
- `entity_aliases` は表記揺れ・別名・略称などを追加するテーブル（Schedulerの LLM抽出名との突合に使う）。
- `unit_entities` は Episode/Fact/Summary 等の Unit に「どのEntityが出たか」を紐付ける（検索・サマリ更新・関係推定に使う）。
- `edges` は Entity間の軽量な関係グラフ（名寄せ/関係推定の材料）。

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

#### 使い方（版管理）

- Workerや管理APIが Unit メタ/派生物を更新する際、変更内容（payload_obj）をハッシュ化して保存する。
- `patch_reason` は更新理由の固定文字列（例: `reflect_episode`, `extract_facts_update`, `admin_update_unit_meta` など）。
- “上書き禁止”というより、「更新の履歴を残す」運用。

## Payloadテーブル

### Episode（証跡：出来事/会話ログ）

```sql
create table if not exists payload_episode (
  unit_id         integer primary key references units(id) on delete cascade,
  input_text      text,
  reply_text      text,
  image_summary   text,
  context_note    text,
  reflection_json text
);
```

#### 使い方（Episode）

- EPISODE は「証跡」枠：ユーザー発話/通知本文/生成結果など、後段の派生処理の元データ。
- `input_text`/`reply_text` は会話ログとして保存され、Retriever（FTS/Vector）に利用される。
- `context_note` はクライアント情報など任意JSON文字列（UI/アプリ名/ウィンドウタイトル等）。
- `reflection_json` は `reflect_episode` のLLM出力（JSON）を丸ごと保存（解析用）。

### Episode FTS（BM25：検索インデックス）

Hybrid Search（Vector + BM25）の BM25 側を担う。詳細は `docs/retrieval.md` を参照。

```sql
-- payload_episode を対象にした FTS5 仮想テーブル（BM25）
-- 注意: external content FTS は INSERT/UPDATE/DELETE に追従するトリガー（または再構築手順）が必要
CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5(
  input_text,
  reply_text,
  content='payload_episode',
  content_rowid='unit_id',
  tokenize='unicode61'
);
```

補足:
- FTSは “索引” のみで、本文の正は `payload_episode`（external content）。
- payload更新に追従するトリガーをDB初期化で作成している（実装: `cocoro_ghost/db.py`）。

### Vector Index（sqlite-vec: vec0）

Vector索引は本文を持たず `unit_id` で `units` / `payload_*` にJOINして本文を取得する。

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS vec_units USING vec0(
  unit_id integer primary key,
  embedding float[<dimension>] distance_metric=cosine,
  kind integer partition key,
  occurred_day integer,
  state integer,
  sensitivity integer
);
```

補足:
- `occurred_day` は `occurred_at // 86400` を保存（検索時の期間フィルタ用）。
- `state` / `sensitivity` は `units` と同期される（検索フィルタ用）。

### Fact（安定知識：証拠リンク）

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

#### カラム詳細（Fact）

| column | 意味/使い方 |
|---|---|
| `subject_entity_id` | 主語（Entity参照）。未確定の場合はNULLになりうる。 |
| `predicate` | 関係の種類（例: likes/prefers/is/has）。スキーマは固定し過ぎず運用で揃える。 |
| `object_text` | 目的語の文字列（Entityで表せない場合に使用）。 |
| `object_entity_id` | 目的語がEntityにできる場合の参照。 |
| `valid_from`/`valid_to` | 有効期間（任意）。古いFactを落とす/矛盾検出に使える。 |
| `evidence_unit_ids_json` | 根拠の Episode unit_id のJSON配列。更新の説明責任のため保持する。 |

### Summary / SharedNarrative（要約）

```sql
create table if not exists payload_summary (
  unit_id      integer primary key references units(id) on delete cascade,
  scope_label  text not null,       -- 自由ラベル（例: shared_narrative/person/topic/...）
  scope_key    text not null,       -- "rolling:7d", "person:123", "topic:unity" ...
  range_start  integer,
  range_end    integer,
  summary_text text not null,
  summary_json text              -- JSON string（LLM出力を丸ごと保存、例: {"summary_text":...,"key_events":[...],"shared_state":...}）
);

create index if not exists idx_summary_scope on payload_summary(scope_label, scope_key);
```

#### 使い方（Summary）

  - `scope_label` と `scope_key` で「どの範囲の要約か」を表す。
    - shared_narrative: `rolling:7d`（直近7日ローリング）
    - person: `person:<entity_id>`
    - topic: `topic:<normalized>`
- `summary_text` は注入用のプレーンテキスト（Schedulerが `<<<COCORO_GHOST_SECTION:SHARED_NARRATIVE>>>` に入れる）。
- `summary_json` はLLM出力を丸ごと保存（key_events等の構造化）。

### OpenLoop（未完了：次に話す理由）

```sql
create table if not exists payload_loop (
  unit_id    integer primary key references units(id) on delete cascade,
  expires_at integer not null,
  due_at     integer,
  loop_text  text not null
);

create unique index if not exists idx_loop_text_unique on payload_loop(loop_text);
create index if not exists idx_loop_due on payload_loop(due_at);
create index if not exists idx_loop_expires on payload_loop(expires_at);
```

#### 使い方（Loop）

- Loopは「未完了の再提起」用途の短期メモ（TTL）で、期限（`expires_at`）を過ぎたものは自動削除する。
- `payload_loop` に存在するものが `<<<COCORO_GHOST_SECTION:OPEN_LOOPS>>>` に注入される。
- `due_at` は再提起の優先度（期限が近いものを先に）。
- `expires_at` はサーバ側で付与する（既定: `due_at` が未来なら `expires_at=due_at`、無ければ `expires_at=now+7日`。上限: 最大30日）。

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

#### カラム詳細（jobs）

| column | 意味/使い方 |
|---|---|
| `kind` | ジョブ種別（例: `reflect_episode`, `extract_entities`, `upsert_embeddings`, `shared_narrative_summary` など）。 |
| `payload_json` | 入力（例: `{"unit_id":123}` / `{"scope_key":"rolling:7d"}`）。 |
| `status` | 0 queued / 1 running / 2 done / 3 failed。 |
| `run_after` | 実行可能時刻（UTC epoch sec）。バックオフに使う。 |
| `tries` | 失敗回数。上限超過で failed に落とす。 |
| `last_error` | 直近失敗のエラー文字列。 |

補足:
- 内蔵Workerが `queued` からclaimして `running` にし、処理後に `done/failed` に遷移させる。
- 埋め込み更新など “同じ入力で何度走っても安全” を前提に設計する（冪等）。

#### どんなときに `jobs` が積まれるか（現行実装）

外部から任意に投入するのではなく、主に以下のイベントで内部enqueueされる。

**A. Episode（会話/通知）保存時の既定ジョブ**

- 対象: `units(kind=EPISODE)` を保存した直後
- enqueue: `reflect_episode` / `extract_entities` / `extract_facts` / `extract_loops` / `upsert_embeddings`
- 入口の例:
  - `/api/chat` の完了時（SSE done直前の保存）
  - `/api/v2/notification` の処理完了時（reply生成後の保存更新）

**B. 背景共有サマリ（現行: `rolling:7d`）の更新**

- 対象: `payload_summary(scope_label=shared_narrative, scope_key=rolling:7d)` を作成/更新する `shared_narrative_summary` job
- Workerの生成内容（現行）:
  - 対象Episode: 直近7日（`occurred_at`）のEPISODEを最大200件（`sensitivity <= SECRET`）
  - 出力: `payload_summary.summary_text`（注入用） + `payload_summary.summary_json`（例: `{summary_text,key_events,shared_state}`）
  - メタ: `units.source=shared_narrative_summary`、更新時は `units.state=VALIDATED`、`range_start/range_end` を保存
- enqueue経路:
  - Episode保存時に必要なら自動enqueue（重複抑制あり / クールダウンあり）
  - 定期実行ユーティリティ（cron無し）から必要ならenqueue（重複抑制あり / クールダウンあり）
  - 判定ロジック（共通の意図）:
    - `shared_narrative_summary` が `queued/running` なら enqueue しない
    - サマリが無い場合は enqueue
    - サマリ最終更新から一定時間（現行: 6h）未満なら enqueue しない
    - 最終更新以降の新規Episode（`occurred_at` があり、`occurred_at > summary.updated_at`）がある場合のみ enqueue

**C. proactive（meta-request起因の能動メッセージ）**

- 対象: `units(kind=EPISODE, source=proactive)` の結果を検索対象にしたい
- enqueue: `upsert_embeddings(unit_id)`（会話ログ同様に検索できれば十分なため）

**D. Worker処理の副作用としての follow-up jobs**

- 例: FACT/LOOP/SUMMARY を新規作成・更新したとき、それ自体を検索対象にするため `upsert_embeddings(unit_id)` を追加enqueueする。
  - （派生Unitを作ったら終わり、ではなく「検索できる状態まで整える」までを非同期で完結させる）

## DBの使い方（読み書きの境界）

### 読み書き主体

- API（同期）
  - Episode保存（`units(kind=EPISODE)` + `payload_episode`）
  - jobs enqueue（反射/抽出/埋め込み等）
- Worker（非同期）
  - 反射（episodeのメタ更新）
  - entity/fact/loop/summary の生成・更新
  - vec0（`vec_units`）の upsert

### 検索に関わるテーブル

- Vector検索: `vec_units`（本文は持たず `unit_id` でJOIN）
- BM25: `episode_fts`（external content）
- 本文の正: `payload_*` と `units`

## Enum定義（実装準拠）

### UnitKind

| name | value | 用途 |
|---|---:|---|
| EPISODE | 1 | 証跡（会話/出来事） |
| FACT | 2 | 安定知識（好み/関係/設定） |
| SUMMARY | 3 | 要約（週次/人物別/トピック別/共有ナラティブ） |
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

### EntityType

固定の EntityType（enum値）は運用で破綻しやすいため廃止し、下記で表現する。

- `entities.type_label`（TEXT）: 自由ラベル（例: `PERSON` / `TOPIC` / `APP` / `CHARACTER` / `GAME` ...）。保存時は大文字に正規化。
- `entities.roles_json`（TEXT: JSON array）: 用途の役割（例: `["person"]` / `["topic"]`）。保存時は小文字に正規化。
  - `person`: person_summary_refresh の対象
  - `topic`: topic_summary_refresh の対象

### RelationType

固定の RelationType（enum値）は運用で破綻しやすいため廃止し、`edges.relation_label`（TEXT）で表現する。

- 推奨ラベル: `friend` / `family` / `colleague` / `romantic` / `likes` / `dislikes` / `related` / `other`
- ただし自由に増やしてよい（例: `mentor` / `manager` / `rival` / `coworker` ...）

### SummaryScopeType

固定の SummaryScopeType（enum値）は運用で破綻しやすいため廃止し、`payload_summary.scope_label`（TEXT）で表現する。

- 推奨ラベル: `shared_narrative` / `person` / `topic`（必要なら `daily` / `monthly` などを追加）

### EntityRole

| name | value | 意味 |
|---|---:|---|
| MENTIONED | 1 | Unit内で言及された |

### JobStatus

| name | value | 意味 |
|---|---:|---|
| QUEUED | 0 | 待機 |
| RUNNING | 1 | 実行中 |
| DONE | 2 | 完了 |
| FAILED | 3 | 失敗 |

## SQLite 推奨PRAGMA（起動時）

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA foreign_keys=ON;
```
