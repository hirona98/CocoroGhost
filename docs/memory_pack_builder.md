# MemoryPack Builder仕様

## 目的

- Scheduling の発想（予測・プリロード・注意機構）を、API運用に合わせて実装する
- **“会話の一貫性”を最優先**しつつ、注入トークンを制御する
- 検索結果をそのまま注入しない。**注入パック（MemoryPack）** を編成して注入する

## 入力

- `user_text`
- `embedding_preset_id`（対象の記憶DB）
- `now_utc`（epoch sec。MemoryPack の `[CONTEXT_CAPSULE]` に `now_local` として注入される。`now_local` はサーバのローカル時刻）
- `client_context`（任意）
- `max_inject_tokens`（プリセット）
- `persona_text` / `addon_text`（settings の active preset。addon は任意の追加オプション）

## 出力：MemoryPack（注入テキスト）

以下の見出し順で構成（LLMの挙動が安定する）

```text
[PERSONA_ANCHOR]
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
- MemoryPack は `memorypack` を system に注入し、conversation に直近会話（max_turns_window）+ user_text を渡す形で LLM に渡される（仕様: `docs/api.md`）。
- MemoryPack は内部注入テキストのため、見出し名や中身をそのままユーザーへ出力しないようにする（ユーザー設定の prompt に書かせず、コード側でガードするのが推奨。例: `cocoro_ghost/memory.py`）。
- `[CONTEXT_CAPSULE]` には `now_local` / `client_context` 等に加え、`partner_mood_state: {...}`（重要度×時間減衰で集約した機嫌）を注入する（実装: `cocoro_ghost/memory_pack_builder.py` / 計算: `cocoro_ghost/partner_mood.py`）。
   - デバッグ用途: `PUT /api/partner_mood` による in-memory ランタイム状態が有効な場合、注入される `partner_mood_state` は適用後の値になる。

## 取得手順（規定）

1. **常時注入（検索しない）**
   - active persona（`settings.db` の `active_persona_preset_id`）
   - active addon（`settings.db` の `active_addon_preset_id`）
2. **Contextual Memory Retrieval（Retriever・LLMレス）**
   - 固定クエリ → Hybrid Search（Vector + BM25）→ ヒューリスティック Rerank（`docs/retrieval.md`）
   - relevant episodes（最大5件）を高速に取得する
3. **Entity解決**
   - 文字列から alias 参照（`entities` + `entity_aliases`）
   - 足りなければLLM抽出（Workerでも可、同期が重い場合は後回し）
   - Current: MemoryPack Builderは alias/name の文字列一致 + 一致が無い場合のみLLMフォールバック
4. **Facts優先取得**
   - 関連entityのfactを信頼度・鮮度・pinでスコアリング
5. **Summaries取得**
   - 週次（BOND）＋該当topic/person
   - Current: BOND週次 + person/topic を注入対象として運用（生成は自動enqueue）
6. **OpenLoops取得**
   - openのみ、due順、entity一致を優先
7. **Episode evidence 注入**
   - `should_inject_episodes(relevant_episodes)` が true のときだけ `[EPISODE_EVIDENCE]` を組み込む
   - `injection_strategy`（quote_key_parts/summarize/full）に応じて整形する
   - Current: Retrieverは `quote_key_parts` 固定
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
  5. PERSONA/ADDON は原則維持（人格崩壊の原因になるため）
