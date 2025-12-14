# Scheduler（取得計画器）仕様

## 目的

- MemOSの Scheduling の発想（予測・プリロード・注意機構）を、API運用に合わせて実装する
- **“会話の一貫性”を最優先**しつつ、注入トークンを制御する
- 検索結果をそのまま注入しない。**注入パック（MemoryPack）** を編成して注入する

## 入力

- `user_text`
- `memory_id`（対象の記憶DB）
- `now_utc`（epoch sec。MemoryPack の `[CONTEXT_CAPSULE]` に `now_local` として注入される。`now_local` はサーバのローカル時刻）
- `client_context`（任意）
- `max_inject_tokens`（プリセット）
- `persona_text` / `contract_text`（settings の active preset）

## 出力：MemoryPack（注入テキスト）

以下の見出し順で構成（LLMの挙動が安定する）

```text
[PERSONA_ANCHOR]
...

[RELATIONSHIP_CONTRACT]
...

[CONTEXT_CAPSULE]
...

[STABLE_FACTS]
- ...

[SHARED_NARRATIVE]
- ...

[OPEN_LOOPS]
- ...

[EPISODE_EVIDENCE]
以下は現在の会話に関連する過去のやりとりです。

[YYYY-MM-DD] タイトル（任意）
User: 「...」
Partner: 「...」
→ 関連: （短い理由）
```

補足:
- MemoryPack は `guard_prompt + memorypack + user_text` の形で LLM に渡される（仕様: `docs/api.md`）。
- MemoryPack は内部注入テキストのため、見出し名や中身をそのままユーザーへ出力しないようにする（ユーザー設定の prompt に書かせず、コード側でガードするのが推奨。例: `cocoro_ghost/memory.py`）。

## 取得手順（規定）

1. **常時注入（検索しない）**
   - active persona（`settings.db` の `active_persona_preset_id`）
   - active contract（`settings.db` の `active_contract_preset_id`）
2. **Contextual Memory Retrieval（Retriever）**
   - Query Expansion → Hybrid Search（Vector + BM25）→ LLM Reranking（`docs/retrieval.md`）
   - relevant episodes（最大5件）を取得する
3. **Entity解決**
   - 文字列から alias 参照（`entities` + `entity_aliases`）
   - 足りなければLLM抽出（Workerでも可、同期が重い場合は後回し）
4. **Facts優先取得**
   - 関連entityのfactを信頼度・鮮度・pinでスコアリング
5. **Summaries取得**
   - 週次（RELATIONSHIP）＋該当topic/person
6. **OpenLoops取得**
   - openのみ、due順、entity一致を優先
7. **Episode evidence 注入**
   - `should_inject_episodes(relevant_episodes)` が true のときだけ `[EPISODE_EVIDENCE]` を組み込む
   - `injection_strategy`（quote_key_parts/summarize/full）に応じて整形する
8. **圧縮**
   - facts: 箇条書き（1件 1〜2行）
   - episodes: 抜粋/要約（最大N件、1件あたり最大M文字）

## スコア（例：facts）

例：関連度 `score` を以下で計算して上位を採用する。

`score = 0.45*confidence + 0.25*salience + 0.20*recency + 0.10*pin_boost`

- `recency`: `exp(-(now-occurred_at)/tau)` など
- `pin_boost`: `pin ? 1.0 : 0.0`

## Token budget（実装指針）

- `max_inject_tokens` を上限としてセクションごとに「予算枠」を持つ
- 予算超過時は、以下の優先順位で落とす
  1. EPISODE_EVIDENCE（まず削る）
  2. OPEN_LOOPS（件数削減）
  3. SHARED_NARRATIVE（段落短縮）
  4. STABLE_FACTS（低スコアを削る）
  5. PERSONA/CONTRACT は原則維持（人格崩壊の原因になるため）
