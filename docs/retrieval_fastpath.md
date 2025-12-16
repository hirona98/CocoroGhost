# Retriever 高速化（LLMレス）改造計画

## 目的

`/api/chat` の同期パスで発生している **Retrieval のLLM待ち**（Query Expansion / Rerank）を排除し、最初のトークンが返るまでの待ち時間を大幅に短縮する。

本計画は「Sudachi等の新規依存を追加せず」「既存の Vector + BM25 + RRF を活かしつつ」、**LLM rerank を軽量スコアリングに置換（B案）**する方針である。

## 現状（ボトルネック）

- `cocoro_ghost/memory.py` の `stream_chat()` は、返信stream開始前に `Retriever.retrieve()` を完了するまで待つ（= retrieval の遅延がそのまま体感遅延になる）。
- `cocoro_ghost/retriever.py` の `retrieve()` は毎回直列で以下を実行している。
  - Phase 1: Query Expansion（LLM JSON）
  - Phase 2: Hybrid Search（Embedding + sqlite-vec + FTS5）
  - Phase 3: Rerank（LLM JSON）
- 直近ログ例では Rerank LLM が最大の支配要因（~10秒）で、Expansion LLM も~4秒かかっている。

## ゴール/非ゴール

### ゴール

- Rerank の LLM 呼び出しを **完全撤去**（LLM推論コスト=0）
- Query Expansion も段階的に LLM を外し、Retrieval全体の待ちを「Embedding + DB」中心にする
- 関連度が低いときは **注入しない**（MemoryPack肥大化による返信遅延も抑える）
- 既存の `Retriever` のインターフェース（戻り値 `list[RankedEpisode]` と `last_injection_strategy`）は維持

### 非ゴール（今回やらない）

- 形態素解析器（Sudachi等）の導入
- “本当の意味理解”による指示語解決（高精度の照応解析）
- Retrievalを非同期化して「先に返信を出し、後から注入」するような大規模アーキ変更

## 方針（要約）

1. **Candidate生成は現状維持**（Embedding + sqlite-vec / FTS5 + RRF）
2. `_rerank()` を LLM ではなく **軽量スコアリング**で並べ替え、上位だけ返す
3. （任意）`_expand_queries()` はまず LLM を止めて `expanded=[]` とし、必要なら “依存なしの簡易展開” を追加
4. 低関連度のときは `[]` を返し注入を抑制（速度/品質の両面）

---

## 詳細設計：LLMレスRerank（B案）

### 入力

- `user_text`: 現在発話
- `recent_conversation`: 直近メッセージ（`_format_recent_conversation()` の形式で十分）
- `candidates`: Phase2の結果（`CandidateEpisode` 列）

### 出力

- `list[RankedEpisode]`（最大 `max_results`）
- `last_injection_strategy`（原則 `quote_key_parts` 固定で開始）

### スコア構成（軽量・依存なし）

Rerankで“やりたいこと”は主に以下：

- RRF上位でも **無関係/重複** を弾く
- “それっぽい”ものを上位に寄せ、注入するかどうかを決める

そのために、以下の3要素を合成する。

| 記号 | 意味 | 取り方 | 目的 |
|---|---|---|---|
| `rrf` | RRF順位/スコア | Phase2のmerge結果（順位でも可） | Vector/BM25の統合信号 |
| `lex` | 文字n-gram類似度 | `query_text` と `episode_text` のDice/Jaccard | “文脈的に同じ話題”の近似 |
| `rec` | recency | `exp(-age_days/tau)` | 最近の会話を少し優遇 |

#### 1) `query_text` / `episode_text`

- `query_text`:
  - `context = _format_recent_conversation(recent_conversation, max_messages=turns*2)`
  - `query_text = f\"{context}\\n---\\n{user_text}\"`（Phase2の `original_query` と同じ発想）
- `episode_text`:
  - `episode_text = f\"{candidate.user_text}\\n{candidate.reply_text}\"`
- 両方とも、スコア計算前に `_compact_text()` 同等で正規化し、文字数上限（例: 1200 chars）をかける（計算を安定させる）。

#### 2) 文字n-gram類似度（日本語対応）

分かち書き不要で安定させるため、**文字n-gram** を用いる。

- 例: n=3（3-gram）
- 類似度: Dice係数を推奨（集合でOK）

```
ngrams(s, n) = { s[i:i+n] for i in range(len(s)-n+1) }
dice(A, B) = 2*|A∩B| / (|A| + |B|)
```

短文（挨拶等）で偶然一致しやすい問題を避けるため、以下の補正を入れる。

- `strength = min(1.0, len(query_ngrams)/30)`（クエリが短いほど `lex` を弱める）
- `lex = dice(query_ngrams, episode_ngrams) * strength`

#### 3) recency

```
age_days = (now_ts - occurred_at) / 86400
rec = exp(-age_days / tau_days)
```

推奨: `tau_days = 45`（最近を少し優遇する程度）

#### 4) rrfの扱い

最小実装は「RRF順位を `1/(1+rank)` に変換」で十分。

より安定させるなら、`_rrf_merge()` 内で計算している `scores[unit_id]` を保持し、0-1へ正規化して使う。

- 推奨: `rrf_norm = score / max_score`（maxで割るだけで十分）

#### 5) 最終スコア（初期値）

まずは以下で開始し、ログを見て調整する。

```
final = 0.55*rrf_norm + 0.35*lex + 0.10*rec
```

### 重複抑制（dedupe）

同じようなエピソード（例: 挨拶ログ）が複数選ばれ、注入が無駄に増えるのを防ぐ。

- 選択済み `episode_text` との `dice(ngrams)` が `dup_threshold` を超えたらスキップ
- 推奨: `dup_threshold = 0.90`

（注: `max_results` は小さいので、逐次選択しながらのO(k^2)チェックで問題ない）

### 注入判定（返す/返さない）

Scheduler側の `should_inject_episodes()` は「highが1つ以上」または「mediumが2つ以上」で注入する。

LLMレス化では“無関係注入”が一番のリスクなので、**強めに注入を絞る**のを推奨。

- `final >= high_threshold` のものが1つでもあれば注入対象
  - top1は `high`、残りは `medium`（ただし `final >= medium_threshold` のものだけ）
- top1が `high_threshold` 未満なら、基本は `[]` を返す（注入しない）

推奨初期値（要チューニング）:

| 変数 | 初期値 | 意図 |
|---|---:|---|
| `high_threshold` | 0.35 | 1件注入の最低ライン |
| `medium_threshold` | 0.28 | 2件目以降の採用ライン |
| `min_results` | 0〜1 | 基本は「自信が無いなら注入しない」 |

### reason の生成

LLM由来の自然文は無くなるので、短く機械的で良い。

- 例: `reason = "heuristic rerank: lex=0.31 rec=0.82 rrf=0.63"`
- または `reason = "テキスト類似度が高い/直近に近い"` の固定文

（MemoryPack内に出るがユーザーへ開示しない前提。長くしない。）

### injection_strategy

まずは `quote_key_parts` 固定で開始する（注入量を最小化して速度優先）。

将来的に必要なら、`final` や選択件数で切り替える:

- `final` が非常に高い & 1件のみ → `full`
- 3件以上 → `quote_key_parts`
- 予算逼迫（`max_inject_tokens` が小さい等） → `summarize`

---

## Query Expansion（LLMレス化の扱い）

Rerankだけ外しても、Expansion LLMが残ると待ちが残る。速度最優先なら段階的に外す。

### Phase A（最初にやる推奨）：expandedを空にする

- `_expand_queries()` は即return `[]`
- 検索クエリは `original_query = context + user_text` の1本のみ
- これだけで Retrieval のLLM待ちはゼロになる

品質低下が出たら次へ進む。

### Phase B（必要になったら）：依存なしの簡易展開 → やらない

形態素解析なしで、以下の“軽い”展開だけ入れる。

- `user_text` から「長めの連続文字列」を抽出（漢字/カタカナ/英数字の連続など）
- `recent_conversation` からも同様に抽出し、直近に出た固有っぽい語を補助クエリにする
- ストップワード（それ/あれ/これ/さっき/この前 等）を除外
- 最大3〜5件、短すぎる語（2文字以下）を捨てる

※ “照応解析”はやらず、「検索候補を増やす」程度に留める。

---

## 実装手順（コード改造計画）

### 0. 計測を入れる（必須）

目的: 調整のために「どこに何msかかっているか」「注入件数/スコア分布」を把握する。

- `Retriever.retrieve()` で各フェーズの経過時間を DEBUG ログ（または event）に出す
- `memory.py` の MemoryPack生成〜LLM返信開始までの内訳も同様に出す

### 1. RRFスコアを保持できるようにする

対象: `cocoro_ghost/retriever.py`

- `_rrf_merge()` を「id配列」だけでなく「id→rrf_score」も返せるようにする
- `CandidateEpisode` に `rrf_score: float` を追加（または別マップで持つ）

### 2. `_rerank()` を heuristic 実装に差し替える

対象: `cocoro_ghost/retriever.py`

追加するユーティリティ（例）:

- `_char_ngrams(text: str, n: int, limit: int) -> set[str]`
- `_dice(a: set[str], b: set[str]) -> float`
- `_recency(occurred_at: int, now_ts: int, tau_days: float) -> float`

置換内容:

- LLM呼び出し（`generate_json_response`）を撤去
- `final` でソート、重複排除、閾値判定、`RankedEpisode` 生成
- `last_injection_strategy` を `quote_key_parts` に設定

### 3. `_expand_queries()` の LLM を止める（速度優先）

対象: `cocoro_ghost/retriever.py`

段階:

- Phase A: 即 `[]` を返す（最小）
- Phase B: 必要なら簡易展開を追加（依存なし）

### 4. フィーチャーフラグ/ロールバック手段を用意 → 用意しない。不要な処理は削除。互換性は不要。

品質の揺れに備えて、簡単に戻せる仕組みを用意する。

案:

- 環境変数で切替（例: `COCORO_RETRIEVER_MODE=llm|heuristic`）
- もしくは settings.db の GlobalSettings に JSONフィールド追加（UI/API連携は後回し）

最初は「コード上の定数＋環境変数」から開始が安全。

### 5. 検証

最低限:

- 既存の `tests/test_api.py` で `/chat` の疎通を確認（手動）

追加推奨（軽量ユニットテスト）:

- n-gram類似の挙動（短文/長文/日本語/英語）
- dedupeが効くこと（同文候補が複数あるケース）
- 閾値で注入が抑制されること（無関係ケースで `[]` になる）

### 6. チューニング（運用ログから）

見るべきログ:

- `final/lex/rrf/rec` の分布
- 注入件数（0/1/2/5の割合）
- 返信の体感（注入0の時に返信が速くなるか）

調整指針:

- 無関係注入が多い → `high_threshold` を上げる、`strength` を強める、dedupeを厳しくする
- 想起が弱い → `lex` の重みを上げる、簡易expand（Phase B）を導入
- 最近の話が出ない → `rec` の重み/`tau` を調整

---

## 期待効果（目安）

- Rerank LLM（~10秒）: **0ms**
- Expansion LLM（~4秒）: Phase Aで **0ms**
- 残る主な待ち: Embedding API + DB検索 + MemoryPack生成

注入抑制が効けば、返信生成の入力トークンも減り、全体レイテンシがさらに下がる可能性がある。

