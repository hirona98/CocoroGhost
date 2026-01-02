# AI人格の反射/機嫌（partner_affect / partner_mood）仕様

このドキュメントは「会話の内容にその時の反射（affect）と、その後も続く機嫌（mood）を反映する」ための実装方針と、保存場所・注入場所をまとめます。

## 目的

- **即時反応（affect）**: 特定の発言で、そのターンの返答が苛立ったり喜んだりできる
- **持続（mood）**: 大事件（重要度が高い出来事）は数ターンで消えず、余韻として残る
- **口調だけに閉じない**: “協力/拒否” などの行動方針にも影響させられる
- **通常はユーザー介入なし**: UI操作で状態を直接上書きしない（ただしデバッグ用途では例外として専用APIで一時上書きを許可する）

## 全体像（2層）

1) **即時（そのターン）**: `/api/chat` の同一LLM呼び出しで「返答本文 + 内部JSON（反射）」を生成し、内部JSONをサーバ側で回収して保存・反映する  
2) **持続（次ターン以降）**: 過去エピソードの反射値を「重要度×時間減衰」で積分し、現在の機嫌（mood）を推定して `CONTEXT_CAPSULE` に注入する

## 即時反応（partner_affect trailer）

### 出力形式（LLM → サーバ）

`/api/chat` では、返答本文の直後に次の区切り文字を1行で出力し、その次行に JSON を1つだけ出力します。

- 区切り文字: `<<<COCORO_GHOST_PARTNER_AFFECT_JSON_v1>>>`

サーバ側は区切り以降を **SSEに流さず** 回収し、`units` / `payload_episode` に即時反映します（実装: `cocoro_ghost/memory.py`）。

### 内部JSON（partner_affect trailer）

JSONスキーマは `docs/prompts.md` の「chat（SSE）: 返答末尾の内部JSON（partner_affect trailer）」を参照してください。

## 持続（重要度×時間減衰の集約 → partner_mood）

直近N件の平均では「大事件が短時間で埋もれる」ため、エピソードごとに影響度を定義して積分します。

### 影響度（エピソード i）

`impact_i = partner_affect_intensity_i × salience_i × confidence_i × exp(-Δt / τ_i)`

- `partner_affect_intensity`（0..1）: その瞬間の感情反応の強さ
- `salience`（0..1）: 出来事の重要度（高いほど残りやすくする）
- `confidence`（0..1）: 推定の確からしさ（不確実なら影響を弱める）
- `Δt`（秒）: 今から見た経過時間
- `τ_i`（秒）: 残留時間（salienceに依存して可変）

### 残留時間 τ（salience依存）

`τ(s) = τ_min + (τ_max - τ_min) × s^k`

既定（実装: `cocoro_ghost/partner_mood.py`）:

- `τ_min = 120s`（雑談が消える最短スケール）
- `τ_max = 6h`（大事件の余韻の最大スケール）
- `k = 2.0`（高salienceだけ急に伸びる）

### 現在の機嫌（ラベル + 強度）

- ラベルごとに `Σ impact_i` を積み上げ（joy/sadness/anger/fear）
- 合計は 0..∞ になりうるため、`1 - exp(-x)` で 0..1 に圧縮
- 最大成分を `label`、最大値を `intensity` とする
- 微小ノイズで揺れないよう、十分小さい場合は `neutral` に戻す（既定: 0.15）

### 行動方針ノブ（partner_response_policy）

内部JSONに `partner_response_policy` がある場合、機嫌（mood）の状態に反映します。

- `refusal_allowed`（bool）: 拒否/渋りを選んでよいゲート
- `refusal_bias`（0..1）: 拒否に寄せる度合い
- `cooperation`（0..1）: 協力に寄せる度合い

#### なぜ label/intensity と分けるのか

`label`/`intensity` は「今の機嫌が何っぽいか（状態の要約）」、`partner_response_policy` は「返答生成での振る舞い（行動方針のノブ）」で役割が違うため分けています。

- `label` が同じでも、常に同じ行動（拒否/協力）にしたいとは限らない
  - 例: `anger` でも “言い方が冷たいだけ” と “実際に拒否/渋る” は別
- 1つのスカラーに潰すと、後段で結局しきい値・カーブ・例外が必要になり、調整が難しくなりがち
- `refusal_allowed` は連続量というより安全弁（「拒否してよいか」を安定して切り替えるため）

#### refusal_allowed は誰がどうやって決めるか

`refusal_allowed` には、次の3つの経路があります。

1) **通常（自動計算）**

過去エピソードから集約した `anger` 成分が十分高いときに `refusal_allowed=true` になります。

- `anger` は joy/sadness/anger/fear の積分（重要度×時間減衰）の結果を 0..1 に圧縮した値
- 既定のゲートは `anger >= 0.75`

2) **LLMの内部JSON（partner_affect trailer）による間接/直接の制御**

`/api/chat` の内部JSONは Episode に保存され、次ターン以降の機嫌計算に取り込まれます。

- 間接: `partner_affect_label/intensity/salience/confidence` によって `anger` が上がれば、結果として `refusal_allowed` が立ちやすくなる
- 直接（上書き候補）: 内部JSONに `partner_response_policy.refusal_allowed` を含めた場合、
  - 「直近で重要な出来事（salience×confidence×時間減衰）が強い」ときほど採用されやすい
  - bool は暴れやすいので、重みが強いときのみ上書きする（弱いときは自動計算を優先）

3) **デバッグ用API（in-memory override）による強制上書き**

UI/デバッグ用途では `PUT /api/partner_mood` で `response_policy` を含む状態を完全上書きできます。

- 永続化はしない（プロセス内のみ）
- override がある場合は計算結果に対して完全上書きが適用される

## 保存場所 / 注入場所

- 保存（素材）:
  - `units.partner_affect_label` / `units.partner_affect_intensity` / `units.salience` / `units.confidence` / `units.topic_tags`
  - `/api/chat` は partner_affect trailer で即時更新（`payload_episode.reflection_json` にも保存）
  - その他入口（notification/capture 等）は Worker `reflect_episode` が補完（反射済みならスキップ）
- 注入:
  - 同期: `cocoro_ghost/memory_pack_builder.py::build_memory_pack()` が `CONTEXT_CAPSULE` に `partner_mood_state: {...}` を追加
  - 非同期: `cocoro_ghost/worker.py::_handle_capsule_refresh()` が `payload_capsule.capsule_json.partner_mood_state` を更新

## デバッグ用：ランタイム状態（in-memory override）

UIから機嫌（partner_mood）を一時的に参照/変更するため、in-memory のランタイム状態を提供します。

- API: `GET /api/partner_mood` / `PUT /api/partner_mood`（仕様: `docs/api.md`）
- override解除: `DELETE /api/partner_mood`（自然計算に戻す）
- `GET` は「前回チャットで使った値（last used）」を返す（無ければデフォルト値）。DBからの計算はしない
- 永続化: しない（DBにも `settings.db` にも保存しない）
- 注意:
  - 同一プロセス内の in-memory 状態なので、プロセス再起動で消える
  - 複数プロセス/複数ワーカー構成ではプロセスごとに状態が分離される

## 失敗時の挙動（フォールバック）

- LLMが区切り文字/JSONを出さない場合、サーバ側は内部JSONを取得できない
  - その場合でも Episode は保存され、Worker `reflect_episode` が後から反射値を埋める（次ターン以降で回復）
- 区切り文字が本文中に誤って混入した場合、サーバは最初に出現した区切りで分離する（設計上の前提: 本文に区切りは出さない）
