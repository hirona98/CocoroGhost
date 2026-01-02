# LLM呼び出し仕様（プロンプト・JSONスキーマ）

プロンプトが「どの処理のどこで」使われるか（フロー図）は `docs/prompt_usage_map.md` を参照。

## 共通ルール

- 出力は **必ずJSON**（前後に説明文を付けない）
- 推測が不確実なら conservative（例: 不確実な項目は出力しない / `confidence`低め）
- JSONは決められた key を使い、型を守る（`null` は許可）

## 記憶検索（Retriever）

記憶検索（Retriever）は **LLMを使用しない**。詳細は `docs/retrieval.md` を参照。

- Query Expansion: 廃止（固定2クエリに置換）
- LLM Reranking: 廃止（ヒューリスティック Rerank に置換）
- Intent 分類: 廃止（常時検索に移行）

## Reflection（Episode派生）

### 出力JSON

```json
{
  "reflection_text": "string",
  "persona_affect_label": "joy|sadness|anger|fear|neutral",
  "persona_affect_intensity": 0.0,
  "topic_tags": ["仕事", "読書"],
  "salience": 0.0,
  "confidence": 0.0
}
```

- `units` の `persona_affect_* / salience / confidence` に反映
- `payload_episode.reflection_json` に保存
- 数値の範囲（推奨・実装もこの前提で扱う）:
  - `persona_affect_intensity`: 0.0〜1.0
  - `salience`: 0.0〜1.0
  - `confidence`: 0.0〜1.0

## chat（SSE）: 返答末尾の内部JSON（persona_affect trailer）

`/api/chat` は、**同一のLLM呼び出し**で「ユーザー表示本文」と「内部用の反射JSON」を同時に生成する。

- 返答本文の末尾に区切り文字 `<<<COCORO_GHOST_PERSONA_AFFECT_JSON_v1>>>` を出力し、その次行にJSONを1つだけ出力する
- サーバ側は区切り以降をSSEに流さず回収し、Episodeへ即時反映する（`cocoro_ghost/memory.py`）

### 出力JSON（persona_affect trailer）

```json
{
  "reflection_text": "string",
  "persona_affect_label": "joy|sadness|anger|fear|neutral",
  "persona_affect_intensity": 0.0,
  "topic_tags": ["仕事", "読書"],
  "salience": 0.0,
  "confidence": 0.0,
  "persona_response_policy": {
    "cooperation": 0.0,
    "refusal_bias": 0.0,
    "refusal_allowed": true
  }
}
```

- `persona_affect_label/persona_affect_intensity` は **PERSONA_ANCHORの人物側の感情反応（affect）**（ユーザーの感情推定ではない）
- `salience` は「重要度×時間減衰」集約の係数（重要な出来事ほど長く残す）
- `persona_affect_intensity/salience/confidence` は 0.0〜1.0
- `persona_response_policy` は口調だけでなく「協力/拒否」などの行動方針に効かせるための内部ノブ
  - 実装では `persona_mood_state.response_policy`（次ターン以降の注入）にも反映される

## Entity抽出

### 出力JSON

```json
{
  "entities": [
    {"type_label": "PERSON", "roles": ["person"], "name": "string", "aliases": ["..."], "confidence": 0.0},
    {"type_label": "PLACE", "roles": [], "name": "string", "aliases": [], "confidence": 0.0}
  ],
  "relations": [
    {"src": "PERSON:太郎", "relation": "friend", "dst": "PERSON:次郎", "confidence": 0.0, "evidence": "short quote"}
  ]
}
```

- `entities` / `entity_aliases` / `unit_entities` / `edges` を upsert
- `relations.relation` は自由ラベル（推奨: `friend|family|colleague|romantic|likes|dislikes|related|other`）
- `type_label` / `src` / `dst` の TYPE は大文字推奨（内部でも大文字に正規化して保存する）
- `roles` は小文字推奨（内部でも小文字に正規化して保存する）
- `confidence` は 0.0〜1.0

## Fact抽出（安定知識）

### 出力JSON

```json
{
  "facts": [
    {
      "subject": {"type_label": "PERSON", "name": "SPEAKER"},
      "predicate": "prefers",
      "object_text": "静かなカフェ",
      "object": {"type_label": "PLACE", "name": "静かなカフェ"},
      "confidence": 0.0,
      "validity": {"from": null, "to": null}
    }
  ]
}
```

- 保存は `units(kind=FACT)` + `payload_fact`
- `payload_fact.evidence_unit_ids_json` に元 episode の `unit_id` を必ず含める
- `predicate` は語彙爆発を避けるため制御語彙に寄せる（例: `name_is`, `is_addressed_as`, `likes`, `prefers`, `uses`, `owns`, `affiliated_with`, `located_in`, `operates_on`, `goal_is`, `first_met_at` など）

## OpenLoops抽出

### 出力JSON

```json
{
  "loops": [
    {"due_at": null, "loop_text": "次回、UnityのAnimator設計の続きを話す", "confidence": 0.0}
  ]
}
```

- 保存は `units(kind=LOOP)` + `payload_loop`
- `due_at` は `null` または UNIX秒（int）
- `confidence` は 0.0〜1.0
- Loopは短期メモ（TTL）として扱うため、サーバ側が `expires_at` を付与して自動削除する（LLMはclose指示を出さない）
  - 既定: `due_at` が未来なら `expires_at=due_at`、無ければ `expires_at=now+7日`
  - 上限: `due_at/expires_at` は最大30日までに丸める

## Bond Summary（絆サマリ）

### 出力JSON

```json
{
  "summary_text": "string",
  "key_events": [{"unit_id": 123, "why": "..."}],
  "bond_state": "string"
}
```

- `units(kind=SUMMARY, scope_label=bond, scope_key=rolling:7d)` + `payload_summary`
- `payload_summary.summary_json` に LLM の出力JSONを丸ごと保存（`summary_text` は注入用のプレーンテキストとして残す）

## Person Summary（人物サマリ）

人物（PERSON）に関する要約を生成する（会話への注入用）。

### 出力JSON（例）

```json
{
  "summary_text": "string",
  "favorability_score": 0.0,
  "favorability_reasons": [{"unit_id": 123, "why": "..."}],
  "key_events": [{"unit_id": 123, "why": "..."}],
  "notes": "optional"
}
```

- `favorability_score` は **PERSONA_ANCHORの人物→人物** の好感度（0..1）。`0.5` を中立として運用する。
- `favorability_reasons` は根拠となる出来事の `unit_id` と短い理由（最大5件）。

## Topic Summary（トピックサマリ）

トピック（TOPIC）に関する要約を生成する（会話への注入用）。

### 出力JSON（例）

```json
{
  "summary_text": "string",
  "key_events": [{"unit_id": 123, "why": "..."}],
  "notes": "optional"
}
```
