# LLM呼び出し仕様（プロンプト・JSONスキーマ）

## 共通ルール

- 出力は **必ずJSON**（前後に説明文を付けない）
- 推測が不確実なら conservative（例: `need_evidence=true` / `confidence`低め）
- JSONは key を固定し、型を守る（`null` は許可）

## Intent分類（small model推奨）

### 出力JSON

```json
{
  "intent": "smalltalk|counsel|task|settings|recall|confirm|meta",
  "need_evidence": true,
  "need_loops": true,
  "suggest_summary_scope": ["weekly", "person", "topic"],
  "sensitivity_max": 1
}
```

### System（固定）

- 出力は必ずJSON
- 推測が不確実なら conservative（need_evidence=true）

## Reflection（Episode派生）

### 出力JSON

```json
{
  "reflection_text": "string",
  "emotion_label": "joy|sadness|anger|fear|neutral",
  "emotion_intensity": 0.0,
  "topic_tags": ["仕事", "読書"],
  "salience_score": 0.0,
  "confidence": 0.0
}
```

- `units` の `emotion_* / salience / confidence` に反映
- `payload_episode.reflection_json` に保存

## Entity抽出

### 出力JSON

```json
{
  "entities": [
    {"etype": "PERSON", "name": "string", "aliases": ["..."], "role": "mentioned", "confidence": 0.0},
    {"etype": "PLACE", "name": "string", "aliases": [], "role": "mentioned", "confidence": 0.0}
  ],
  "relations": [
    {"src": "PERSON:太郎", "rel": "friend", "dst": "PERSON:次郎", "confidence": 0.0, "evidence": "short quote"}
  ]
}
```

- `entities` / `entity_aliases` / `unit_entities` / `edges` を upsert
- `relations.rel` は `friend|family|colleague|partner|likes|dislikes|related|other` を推奨（実装側で `RelationType` にマップする）

## Fact抽出（安定知識）

### 出力JSON

```json
{
  "facts": [
    {
      "subject": {"etype": "PERSON", "name": "USER"},
      "predicate": "prefers",
      "object_text": "静かなカフェ",
      "confidence": 0.0,
      "validity": {"from": null, "to": null}
    }
  ]
}
```

- 保存は `units(kind=FACT)` + `payload_fact`
- `payload_fact.evidence_unit_ids_json` に元 episode の `unit_id` を必ず含める

## OpenLoops抽出

### 出力JSON

```json
{
  "loops": [
    {"status": "open", "due_at": null, "loop_text": "次回、UnityのAnimator設計の続きを話す", "confidence": 0.0}
  ]
}
```

- 保存は `units(kind=LOOP)` + `payload_loop`
- Close条件（任意）：次回会話で完了したと判断したら `status=closed` 更新（版管理で差分）

## Weekly Summary（SharedNarrative）

### 出力JSON

```json
{
  "summary_text": "string",
  "key_events": [{"unit_id": 123, "why": "..."}],
  "relationship_state": "string"
}
```

- `units(kind=SUMMARY, scope_type=RELATIONSHIP, scope_key=2025-W50)` + `payload_summary`
- `payload_summary.summary_json` に LLM の出力JSONを丸ごと保存（`summary_text` は注入用のプレーンテキストとして残す）
