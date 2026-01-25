# ストレージ（SQLite）

## DB境界

- `data/settings.db`
  - トークン / 各種プリセット / 有効フラグ
- `data/reminders.db`
  - リマインダー状態
- `data/memory_<embedding_preset_id>.db`
  - 出来事ログ（`events`）
  - 出来事ログのアシスタント本文要約（`event_assistant_summaries`、選別入力の高速化用）
  - 状態（`state`、`kind` で種別を持つ）
  - 改訂履歴（`revisions`）
  - 検索インデックス（ベクトル/文字n-gram）
  - 観測ログ（`retrieval_runs`）
  - 感情（`long_mood_state` / `event_affects`）

## 方針

- 運用前のため、マイグレーションは扱わない（互換を切って作り直す）
- 埋め込み次元が変わる場合は、`embedding_preset_id` を変えて別DBにする

## テーブル概要（概念）

### `events`（追記ログ）

- 1ターン=1行（user_text + assistant_text）
- `created_at`（記録時刻）と `about_time`（内容の時期）を分けて持つ
- **流れ（文脈）が分かる情報**を持てるようにする
- 画像そのものは保持しない（**画像の説明テキスト**を `events` に残す）
  - 画像付きチャットでは `events.image_summaries_json` に「画像ごとの詳細要約（JSON配列）」を保存する（内部用）

主要カラム:

| カラム | 型 | 説明 |
|--------|------|------|
| event_id | INTEGER | 主キー（自動採番） |
| created_at | INTEGER | 記録時刻（UTC UNIX秒） |
| updated_at | INTEGER | 更新時刻（UTC UNIX秒） |
| searchable | INTEGER | 検索対象フラグ（1=想起対象、0=想起対象外） |
| client_id | TEXT | クライアントID（NULL可） |
| source | TEXT | イベント種別（chat/notification/reminder/desktop_watch/meta_proactive/vision_detail） |
| user_text | TEXT | ユーザー入力（NULL可） |
| assistant_text | TEXT | アシスタント出力（NULL可） |
| image_summaries_json | TEXT | 画像要約（詳細）のJSON配列（内部用、NULL可、要素は最大5） |
| about_start_ts | INTEGER | 内容の開始時刻（UTC UNIX秒、NULL可） |
| about_end_ts | INTEGER | 内容の終了時刻（UTC UNIX秒、NULL可） |
| about_year_start | INTEGER | 内容の開始年（NULL可） |
| about_year_end | INTEGER | 内容の終了年（NULL可） |
| life_stage | TEXT | ライフステージ（elementary/middle/high/university/work/unknown） |
| about_time_confidence | REAL | `about_time` 推定の確信度（0.0〜1.0） |
| entities_json | TEXT | エンティティ抽出結果（JSON配列） |
| client_context_json | TEXT | クライアントコンテキスト（JSON、NULL可） |

注記:

- クライアント入力には文脈IDが無い前提でよい
- 文脈参照（文脈スレッド/返信関係）は、**イベント同士の関係を別テーブルとして構築**して、検索・更新で参照できるようにする
- 同期で張る `reply_to` は「同じ `client_id` の直前チャットイベント」を指す（それ以外は非同期で補正する）

### `event_entities` / `state_entities`（エンティティ索引）

目的:

- `events.entities_json` は監査/表示向けのスナップショットとして残す
- 検索では entity を正規化キー（`entity_type_norm + entity_name_norm`）で引けるように、参照テーブルを別に持つ
- これにより「seed → entity → 関連event/state」の多段想起（entity展開）が作れる

方針:

- WritePlan の `event_annotations.entities` を正規化し、`event_entities` を event_id 単位で作り直す（delete→insert）
- WritePlan の `state_updates[*].entities` を正規化し、`state_entities` を state_id 単位で作り直す（delete→insert）
- 運用前のためマイグレーションは扱わない（DB作り直し前提）

主要カラム（概念）:

| カラム | 型 | 説明 |
|--------|------|------|
| id | INTEGER | 主キー（自動採番） |
| event_id / state_id | INTEGER | 紐づけ先（FK、CASCADE） |
| entity_type_norm | TEXT | 種別（`person/org/place/project/tool`） |
| entity_name_raw | TEXT | 元の表記（監査/表示用） |
| entity_name_norm | TEXT | 正規化した表記（検索用キー） |
| confidence | REAL | 確信度（0.0〜1.0） |
| created_at | INTEGER | 付与時刻（UTC UNIX秒） |

### `event_assistant_summaries`（選別入力向けの派生要約）

目的:

- `SearchResultPack` の「選別」入力を軽量化して、SSE開始までの体感速度を改善する

方針:

- `events.assistant_text` の要約を **派生情報**として保持する（本文の代替にはしない）
- `events.updated_at` と突き合わせて整合性を取る（本文が更新されたら作り直す）
- 運用前のためマイグレーションは扱わない（作り直し前提）

主要カラム:

| カラム | 型 | 説明 |
|--------|------|------|
| event_id | INTEGER | 主キー（eventsと1:1、FK） |
| summary_text | TEXT | アシスタント本文の要約（選別用） |
| event_updated_at | INTEGER | events.updated_at（整合性チェック用） |
| created_at | INTEGER | 作成時刻（UTC UNIX秒） |
| updated_at | INTEGER | 更新時刻（UTC UNIX秒） |

### `event_threads` / `event_links`（文脈グラフ）

出来事ログ（`events`）の「流れ」を後から参照するための内部情報。
必要に応じて推定/更新する。

最小の考え方:

- `event_threads`: イベントを「文脈の束」に所属させる（1つのイベントが複数文脈スレッドに属してもよい）
- `event_links`: イベント間の関係（reply_to/continuation/caused_by/same_topic など）を張る

この情報は「検索」と「更新」の両方で使える:

- 検索: 「いまの話題の文脈スレッドを辿る」「似た文脈スレッドを拾う」
- 更新: 「同一文脈の状態更新に寄せる」「矛盾の分離（別スレッド扱い）」など

方針:

- 文脈グラフは **保存して育てる**のを正とする（都度推定だけにしない）
- 理由は、(1) 推定の揺れを減らす、(2) 検索を高速/安定化できる、(3) 「なんの文脈だっけ？」の再現性が上がるため
- ただしこれは派生情報なので、必要なら再構築できる（運用前・移行処理なしの前提でも「作り直し」で対応できる）

更新タイミング（正）:

- **同期（返答前）**: 超軽量な仮置きのみ（LLMは使わない）
  - 例: `reply_to = 直前イベント` を張る
- **非同期（返答後）**: 本更新（LLM可、品質最優先）
  - 文脈スレッドの分割/統合
  - `same_topic/caused_by/continuation` などのリンク追加/修正
  - 確信度（`confidence`）を更新し、根拠イベントを持たせて改訂履歴（`revisions`）に残す

保存時の要件（概念）:

- リンク/文脈スレッドには `confidence` と `created_at` を持つ
- 更新は改訂履歴（`revisions`）に残せるよう、根拠イベント（どの `event_id` からそう判断したか）を持つ

#### 保持方法

文脈グラフは派生情報だが、検索の再現性と速度のために `data/memory_<embedding_preset_id>.db` に保存する。

最低限の保存イメージ:

- `event_links`: `from_event_id` → `to_event_id` の関係（`label` と `confidence` と `created_at`）
- `event_threads`: 「イベントがどの文脈スレッドに属するか」（`thread_key` と `confidence` と `created_at`）

#### `thread_key` の採番ルール

- `thread_key` は LLM が決定する文字列
- 新規スレッドは「新しいトピックを表す識別子」として LLM が命名
- LLM は「既存スレッドに寄せる」か「新規スレッドを切る」かを判断

注記:

- 文脈グラフは育つ（分割/統合される）ため、更新は改訂履歴（`revisions`）で追えるようにする

#### 文脈の分離

基本方針は「広めにまとめて、必要なら非同期で分離/統合」。
同期では `reply_to = 直前event_id` の仮置きだけにし、文脈スレッドの分離は非同期の `WritePlan` に任せる。

分離/統合の判断材料（例）:

- 直近の文脈スレッドとの類似（ベクトル/文字n-gram/エンティティ一致）
- 時間ギャップ（長時間の空白）
- 話題転換の明示（「ところで」「別件」など）
- `client_context` の変化（作業/画面の切替など）
- `about_time` の不一致（過去回想に飛んだ等）

### 状態（更新で育つ）

最小セット:

- `state`（単一テーブル、`kind` で種別を表現）

共通で持ちたい列:

- `valid_from_ts` / `valid_to_ts`（並存のための有効期間）
- `last_confirmed_at`（最近性）
- `confidence` / `salience`（検索順位に使える）
- `searchable`（検索対象フラグ、誤想起の分離で0になる）

### 改訂履歴（`revisions`）

- 状態/派生情報（文脈グラフ/感情など）の更新が発生したときに追記
- 変更前/変更後のスナップショット + `reason` + `evidence_event_ids`

主要カラム:

| カラム | 型 | 説明 |
|--------|------|------|
| revision_id | INTEGER | 主キー（自動採番） |
| entity_type | TEXT | 対象種別（state/event_links/event_threads/event_affects） |
| entity_id | INTEGER | 対象レコードのID |
| before_json | TEXT | 変更前のスナップショット（JSON、新規時はNULL） |
| after_json | TEXT | 変更後のスナップショット（JSON） |
| reason | TEXT | 変更理由（短文） |
| evidence_event_ids_json | TEXT | 根拠イベントID（JSON配列） |
| created_at | INTEGER | 作成時刻（UTC UNIX秒） |

対象範囲（正）:

- **revisions対象**: `state`, `event_links`, `event_threads`, `event_affects`
- **revisions対象外**: `events`（追記ログ）, `retrieval_runs`（観測ログ）

注記:

- テーブル名は `revisions` とする
- 通常の会話検索は「出来事ログ（`events`）/状態（`state`）」を中心にし、`revisions` は「なぜ変えた？」の説明・デバッグに寄せる

### 観測ログ（`retrieval_runs`）

検索（思い出す）の「なぜそうなったか」を追えるようにするためのログ。

主要カラム（概念）:

| カラム | 型 | 説明 |
|--------|------|------|
| run_id | INTEGER | 主キー（自動採番） |
| event_id | INTEGER | 対象イベント（FK） |
| created_at | INTEGER | 実行時刻（UTC UNIX秒） |
| plan_json | TEXT | RetrievalPlan（SearchPlan互換、ルール生成） |
| candidates_json | TEXT | 候補の統計（件数/ソース内訳など。本文は保持しない） |
| selected_json | TEXT | SearchResultPack（selectedと理由） |

注記:

- `candidates_json` は「候補本文」ではなく、候補数や `hit_sources` を中心とした観測用データに限定する（ログ肥大化を防ぐ）。

### 文字検索（文字n-gram / FTS5 `trigram`）

- `events` を対象にする（表記一致の補助）
- 日本語は分かち書きが不安定になりがちなので、文字n-gram等を使う（例: 文字3-gram）

注記:

- ここはスコア方式の呼称に寄せず、**文字n-gramによる表記一致インデックス**として扱う。
- SQLite の実装としては、FTS5 の `trigram` を使うのが最小で強い。

### ベクトル検索

- `events` と「状態」の両方を対象にする
- ベクトルは「索引」であり本文は別テーブルに持つ

### 感情

- `long_mood_state`: 長期的な感情（`state` テーブルの `kind="long_mood_state"` として保存）
- `event_affects`: 瞬間的な感情/内心（専用テーブル、イベントごと）

#### `event_affects` テーブル

| カラム | 型 | 説明 |
|--------|------|------|
| id | INTEGER | 主キー（自動採番） |
| event_id | INTEGER | 紐づくイベントID（FK） |
| created_at | INTEGER | 作成時刻（UTC UNIX秒） |
| moment_affect_text | TEXT | 瞬間感情のテキスト表現 |
| moment_affect_labels_json | TEXT | 感情ラベル（JSON配列。WritePlanの `moment_affect_labels` をJSON化して保存） |
| inner_thought_text | TEXT | 内心メモ（NULL可） |
| vad_v | REAL | 快・不快（-1.0〜+1.0） |
| vad_a | REAL | 覚醒（-1.0〜+1.0） |
| vad_d | REAL | 主導（-1.0〜+1.0） |
| confidence | REAL | 確信度（0.0〜1.0） |

方針:

- 「後で足す」が難しい前提なので、**文章＋数値の両方**を最初から持てる形にする
- `moment_affect_labels`（配列）をDBで扱いやすくするため、SQLiteでは `moment_affect_labels_json`（JSON配列文字列）として保持する（専用テーブル化よりシンプルで、検索/デバッグにも使いやすい）
- 数値は厳密さよりも「連続的に変化し、検索/制御に使える」ことを優先する
- VAD（快・不快/覚醒/主導）は **-1.0..+1.0** に固定する
- `event_affects` は **ベクトル検索の対象**（`vec_items.kind=3`）
- 1イベントにつき1件として扱い、既存があれば更新する（重複を避ける）

## 検索のための索引方針（概念）

「保存したのに検索できない」を避けるため、どの情報をどの索引で拾うかを最初に決める。

### ベクトル索引（`vec_items`）

| kind | 値 | 対象 | 備考 |
|------|-----|------|------|
| 1 | `_VEC_KIND_EVENT` | `events` | 出来事ログ本文 |
| 2 | `_VEC_KIND_STATE` | `state` | 状態本文（fact/relation/task/summary/long_mood_state） |
| 3 | `_VEC_KIND_EVENT_AFFECT` | `event_affects` | 瞬間感情/内心 |

`item_id` の計算: `kind * 10,000,000,000 + entity_id`

### 文字n-gram索引（FTS5 `trigram`）

- 対象: `events`（出来事ログ本文）
- 用途: 固有名詞/型番/表記揺れ対策

### 索引対象まとめ

| 対象 | ベクトル | 文字n-gram | 文脈グラフ |
|------|---------|------------|------------|
| events | ○ | ○ | ○ |
| state | ○ | - | - |
| event_affects | ○ | - | - |
| event_threads | - | - | ○ |
| event_links | - | - | ○ |
| revisions | - | - | - |

注記:

- 全項目を主要経路で検索するとノイズとコストが増えるため、RetrievalPlan（ルール生成）と「候補上限/経路クォータ（TOML）」で、混入量を制御する
- ただし原則は「広め」を正とし、ベクトル/文字n-gram/文脈グラフは基本ONにする（最後の選別でノイズを落とす）
