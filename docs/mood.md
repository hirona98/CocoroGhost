# パートナーの機嫌（mood）仕様

このドキュメントは「会話の内容にその時の機嫌（喜怒哀楽）を反映する」ための実装方針と、計算式・保存場所・注入場所をまとめます。

## 目的

- **即時反応**: 特定の発言で、そのターンの返答が怒ったり喜んだりできる
- **持続**: 大事件（重要度が高い出来事）は数ターンで消えず、余韻として残る
- **口調だけに閉じない**: “協力/拒否” などの行動方針にも影響させられる
- **ユーザー介入なし**: UI操作で mood を直接上書きしない

## 全体像（2層）

1) **即時（そのターン）**: `/api/chat` の同一LLM呼び出しで「返答本文 + 内部JSON（反射）」を生成し、内部JSONをサーバ側で回収して保存・反映する  
2) **持続（次ターン以降）**: 過去エピソードの反射値を「重要度×時間減衰」で積分し、現在の機嫌を推定して `CONTEXT_CAPSULE` に注入する

## 即時反応（mood trailer）

### 出力形式（LLM → サーバ）

`/api/chat` では、返答本文の直後に次の区切り文字を1行で出力し、その次行に JSON を1つだけ出力します。

- 区切り文字: `<<<COCORO_GHOST_INTERNAL_JSON_v1>>>`

サーバ側は区切り以降を **SSEに流さず** 回収し、`units` / `payload_episode` に即時反映します（実装: `cocoro_ghost/memory.py`）。

### 内部JSON（mood trailer）

JSONスキーマは `docs/prompts.md` の「chat（SSE）: 返答末尾の内部JSON（mood trailer）」を参照してください。

## 持続（重要度×時間減衰の集約）

直近N件の平均では「大事件が短時間で埋もれる」ため、エピソードごとに影響度を定義して積分します。

### 影響度（エピソード i）

`impact_i = emotion_intensity_i × salience_i × confidence_i × exp(-Δt / τ_i)`

- `emotion_intensity`（0..1）: その瞬間の感情の強さ
- `salience`（0..1）: 出来事の重要度（高いほど残りやすくする）
- `confidence`（0..1）: 推定の確からしさ（不確実なら影響を弱める）
- `Δt`（秒）: 今から見た経過時間
- `τ_i`（秒）: 残留時間（salienceに依存して可変）

### 残留時間 τ（salience依存）

`τ(s) = τ_min + (τ_max - τ_min) × s^k`

既定（実装: `cocoro_ghost/mood.py`）:

- `τ_min = 120s`（雑談が消える最短スケール）
- `τ_max = 6h`（大事件の余韻の最大スケール）
- `k = 2.0`（高salienceだけ急に伸びる）

### 現在の機嫌（ラベル + 強度）

- ラベルごとに `Σ impact_i` を積み上げ（joy/sadness/anger/fear）
- 合計は 0..∞ になりうるため、`1 - exp(-x)` で 0..1 に圧縮
- 最大成分を `label`、最大値を `intensity` とする
- 微小ノイズで揺れないよう、十分小さい場合は `neutral` に戻す（既定: 0.15）

### 行動方針ノブ（拒否を許容）

怒り（anger）が高いときに「協力しにくい/拒否しやすい」方に寄せるため、次を導出します。

- `refusal_allowed = (anger >= 0.75)`（拒否を選んでよいゲート）
- `refusal_bias`（0..1）: 怒りが 0.55 を超え始めたら立ち上げ
- `cooperation`（0..1）: `refusal_bias` とトレードオフ

## 保存場所 / 注入場所

- 保存（素材）: `units.emotion_label` / `units.emotion_intensity` / `units.salience` / `units.confidence` / `units.topic_tags`
  - `/api/chat` は mood trailer で即時更新（`payload_episode.reflection_json` にも保存）
  - その他入口（notification/capture 等）は Worker `reflect_episode` が補完（反射済みならスキップ）
- 注入:
  - 同期: `cocoro_ghost/memory_pack_builder.py::build_memory_pack()` が `CONTEXT_CAPSULE` に `partner_mood: {...}` を追加
  - 非同期: `cocoro_ghost/worker.py::_handle_capsule_refresh()` が `payload_capsule.capsule_json.partner_mood` を更新

## 失敗時の挙動（フォールバック）

- LLMが区切り文字/JSONを出さない場合、サーバ側は内部JSONを取得できない
  - その場合でも Episode は保存され、Worker `reflect_episode` が後から反射値を埋める（次ターン以降で回復）
- 区切り文字が本文中に誤って混入した場合、サーバは最初に出現した区切りで分離する（設計上の前提: 本文に区切りは出さない）

