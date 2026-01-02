# MemoryPack Builder仕様

## 目的

- Scheduling の発想（予測・プリロード・注意機構）を、API運用に合わせて実装する
- **“会話の一貫性”を最優先**しつつ、注入トークンを制御する
- 検索結果をそのまま注入しない。**注入パック（MemoryPack）** を編成して注入する

## 入力

- `input_text`
- `image_summaries`（任意）
- `client_context`（任意）
- `relevant_episodes`（Retrieverの検索結果）
- `matched_entity_ids`（LLMのnames only抽出＋alias突合で解決したentity_id群）
- `now_utc`（epoch sec。MemoryPack の `<<<COCORO_GHOST_SECTION:CONTEXT_CAPSULE>>>` に `now_local` として注入される。`now_local` はサーバのローカル時刻）
- `max_inject_tokens`（プリセット）

## 出力：MemoryPack（注入テキスト）

以下の見出し順で構成（LLMの挙動が安定する）

```text
<<<COCORO_GHOST_SECTION:CONTEXT_CAPSULE>>>
...

<<<COCORO_GHOST_SECTION:STABLE_FACTS>>>
- ...

<<<COCORO_GHOST_SECTION:SHARED_NARRATIVE>>>
- ...

<<<COCORO_GHOST_SECTION:RELATIONSHIP_STATE>>>
- ...

<<<COCORO_GHOST_SECTION:OPEN_LOOPS>>>
- ...

<<<COCORO_GHOST_SECTION:EPISODE_EVIDENCE>>>
以下は現在の会話に関連する過去のやりとりです。

[YYYY-MM-DDTHH:MM:SS±TZ] タイトル（任意）
Speaker: 「...」
Persona: 「...」
→ 関連: （短い理由）
```

補足:
- MemoryPack は system には入れず、`<<INTERNAL_CONTEXT>>` で始まる assistant メッセージとして conversation に注入する（仕様: `docs/api.md`）。
- PERSONA_ANCHOR は system prompt 側に固定注入する（MemoryPackには含めない）。
- MemoryPack は内部注入テキストのため、見出し名や中身をそのままユーザーへ出力しないようにする（コード側でガードする。例: `cocoro_ghost/memory.py`）。
- `<<<COCORO_GHOST_SECTION:CONTEXT_CAPSULE>>>` には `now_local` / `client_context` 等に加え、`persona_mood_state: {...}`（重要度×時間減衰で集約した機嫌）を注入する（実装: `cocoro_ghost/memory_pack_builder.py` / 計算: `cocoro_ghost/persona_mood.py`）。
   - デバッグ用途: `PUT /api/persona_mood` による in-memory ランタイム状態が有効な場合、注入される `persona_mood_state` は適用後の値になる。
   - LLMに渡す時刻はローカル時刻に変換して注入する（now_local/episode日付/persona_mood_state/capsule_json内の時刻）。
- `<<<COCORO_GHOST_SECTION:SHARED_NARRATIVE>>>` は「共有された物語」を注入するセクション。
   - 週次の bond summary（`scope_key=rolling:7d`。無ければ最新）を1本入れる。
   - 今回の entity に応じて、人物サマリ（`scope_label=person`）やトピックサマリ（`scope_label=topic`）を追加する。
   - 目的は「関係性や背景の継続性」を保ち、会話の一貫性を補強すること。
- `<<<COCORO_GHOST_SECTION:RELATIONSHIP_STATE>>>` は「関係性の数値サマリ」を注入するセクション。
   - 今回のユーザー発話から LLM で抽出された entity のうち、`roles=person` の人物のみを対象とする。
   - 各人物の最新の person summary JSON から `favorability_score` を取り出し、最大5件まで注入する。
   - 目的は「人物ごとの好感度などの数値状態」を会話に反映すること。

## 取得手順（規定）

1. **常時注入（検索しない）**
   - active PersonaPreset（`settings.db` の `active_persona_preset_id`）と active AddonPreset（`active_addon_preset_id`）を連結し、PERSONA_ANCHOR として system prompt 側で固定注入
2. **Contextual Memory Retrieval（Retriever・LLMレス）**
   - 固定クエリ → Hybrid Search（Vector + BM25）→ ヒューリスティック Rerank（`docs/retrieval.md`）
   - relevant episodes（最大5件）を高速に取得する
3. **Entity解決（前処理）**
   - LLMで名前候補のみ抽出（`ENTITY_NAMES_ONLY_SYSTEM_PROMPT`）
   - 抽出名を alias/name と突合して entity_id を解決
   - `build_memory_pack()` には解決済みの `matched_entity_ids` を渡す
4. **Facts優先取得**
   - 関連entityのfactを信頼度・鮮度・pinでスコアリング
5. **Summaries取得**
   - 週次（BOND）＋該当topic/person
   - Current: BOND週次 + person/topic を注入対象として運用（生成は自動enqueue）
6. **OpenLoops取得**
   - 期限切れ（`expires_at <= now_ts`）は除外し、due順、entity一致を優先
   - 期限切れのLoopは Worker の `capsule_refresh` で自動削除される
7. **Episode evidence 注入**
   - `should_inject_episodes(relevant_episodes)` が true のときだけ `<<<COCORO_GHOST_SECTION:EPISODE_EVIDENCE>>>` を組み込む
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
  4. RELATIONSHIP_STATE（丸ごと削除）
  5. STABLE_FACTS（低スコアを削る）
  6. PERSONA_ANCHOR（persona_text + addon_text）は system 側固定のため budget 対象外
