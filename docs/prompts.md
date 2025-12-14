# LLM呼び出し仕様（プロンプト・JSONスキーマ）

## 共通ルール

- 出力は **必ずJSON**（前後に説明文を付けない）
- 推測が不確実なら conservative（例: 不確実な項目は出力しない / `confidence`低め）
- JSONは決められた key を使い、型を守る（`null` は許可）

## Intent分類（廃止予定）

> **注意**: Intent分類は `docs/retrieval.md` の Contextual Memory Retrieval に置き換える方針です。
> 現行の実装には残っているが、新Retriever導入時に削除します。

### 出力JSON（削除予定）

```json
{
  "intent": "smalltalk|counsel|task|settings|recall|confirm|meta",
  "need_evidence": true,
  "need_loops": true,
  "suggest_summary_scope": ["weekly", "person", "topic"],
  "sensitivity_max": 1
}
```

## Query Expansion（Retrieval Phase 1）

> 詳細は `docs/retrieval.md` を参照

### 出力JSON

```json
{
  "expanded_queries": ["展開されたクエリ1", "展開されたクエリ2"],
  "detected_references": [
    {"type": "anaphora|temporal|ellipsis|topic", "surface": "表層形", "resolved": "解決後"}
  ]
}
```

### System

- ユーザー発話と直近の会話履歴から、暗黙に参照されている話題を特定
- expanded_queries は最大5件
- 明確に特定できないものは含めない

## Episode Reranking（Retrieval Phase 3）

> 詳細は `docs/retrieval.md` を参照

### 出力JSON

```json
{
  "relevant_episodes": [
    {"unit_id": 12345, "relevance": "high|medium", "reason": "選択理由"}
  ],
  "injection_strategy": "quote_key_parts|summarize|full"
}
```

### System

- 候補エピソードから現在の会話に関連するものを選別
- `unit_id` は候補に含まれるもののみ（候補外のIDは出力しない）
- relevance が high または medium のもののみ出力
- 最大5件まで

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
