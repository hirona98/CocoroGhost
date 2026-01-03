# sqlite-vec（vec0）設計

## 方針

- sqlite-vec は **v0.1.6+** を使用する（metadata/partition/aux 対応）
- vec0 は「索引」。本文は `payload_*` に置き、`unit_id` でJOINして取得する

## 拡張ロード（Python）

- Python（`sqlite3` / SQLAlchemy）で `enable_load_extension(True)` → `load_extension(...)`
- Windows配布時は拡張DLL/SOの同梱とパス解決が必要

本リポジトリの実装例:

- `cocoro_ghost/db.py` で接続時に `sqlite_vec` パッケージ同梱の `vec0` をロードする
- SQLAlchemy の `connect` イベントで `dbapi_conn.load_extension(<path-to-vec0>)` を実行する

## `vec_units`（kind partition + metadata filtering）

- embedding は `distance_metric=cosine` 推奨
- 本文は置かず、索引として **`unit_id` と `embedding`** を保持する
- partition key：種類（`kind`）で物理分割して高速化
- metadata columns：WHEREでフィルタ可能（sqlite-vec v0.1.6+）

```sql
create virtual table if not exists vec_units using vec0(
  unit_id     integer primary key,
  embedding   float[1536] distance_metric=cosine,

  -- partition key：種類で物理分割して高速化
  kind        integer partition key,

  -- metadata columns：WHEREでフィルタ可能（v0.1.6+）
  occurred_day integer,   -- occurred_at / 86400
  state        integer,
  sensitivity  integer
);
```

> `float[1536]` は例です。実装では `settings.db` の `embedding_dimension` に合わせて作成します。  
> 次元数を変える場合は「別DBを用意 or 再構築」する

## KNN基本クエリ（候補ID＋distance）

`vec0` は `k = :k` を使う設計（`LIMIT` はSQLite 3.41+の注意があり、k推奨）。

```sql
select unit_id, distance
from vec_units
where embedding match :query_embedding
  and k = :k
  and kind = :kind
  and state in (0,1,2)
  and sensitivity <= :max_sensitivity
  and occurred_day between :d0 and :d1
order by distance;
```

## KNN → JOIN 雛形

```sql
with knn as (
  select unit_id, distance
  from vec_units
  where embedding match :q
    and k = :k
    and kind = :kind
    and sensitivity <= :sens
    and state in (0,1,2)
  order by distance
)
select
  u.id,
  u.kind,
  u.occurred_at,
  u.confidence,
  u.salience,
  pe.input_text,
  pe.reply_text,
  pe.image_summary,
  knn.distance
from knn
join units u on u.id = knn.unit_id
left join payload_episode pe on pe.unit_id = u.id
order by knn.distance;
```

## Upsert方針

- `unit_id` をキーとして upsert（同一unitの再埋め込みでも整合）
- `occurred_day/state/sensitivity/kind` は `units` 更新と **同期**して更新する
