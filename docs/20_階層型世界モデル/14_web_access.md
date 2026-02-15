# 階層型世界モデル web_access

## 1. 目的

本書は、`web_access` capability の仕様を定義する。

Phase 5 の目的:

1. `web_access` を capability 共通契約上で定義する
2. `/api/chat` 最終応答の Web 検索制御と分離する

## 2. スコープ

対象:

1. `web_access` operation 定義
2. input/result/effect schema
3. world model 反映規則
4. Tacticalize/Execute/Reflect 接続規約

非対象:

1. 他 capability の仕様
2. `/api/chat` 同期経路の変更
3. 実コード実装詳細

## 3. 前提

1. `web_access` は専用経路にしない（Capability Registry 経由）
2. fail-fast を正とし fallback は入れない
3. `/api/chat` の `reply_web_search_enabled` とは別制御
4. 実行結果は `events` と `wm_*` に循環させる

## 4. `/api/chat` Web検索との分離

1. `/api/chat` 側
   - 返答生成（L3）補助としてのみ Web 検索を使用
   - 制御キーは `llm_preset.reply_web_search_enabled`
2. `web_access` 側
   - 自律ループの ActionTicket として実行
   - registry の `enabled` / operation 定義で制御
3. 両者は設定・タイミング・保存責務を共有しない

## 5. CapabilityDescriptor

```json
{
  "capability_id": "web_access",
  "display_name": "Web Access",
  "enabled": true,
  "version": "1",
  "metadata_json": {},
  "operations": ["search", "open_url", "extract_structured"]
}
```

## 6. operation 仕様（JSON Schema）

共通:

1. `input_schema_json` / `result_schema_json` / `effect_schema_json` は JSON Schema Draft 2020-12 で定義する
2. すべて top-level `type=object` かつ `additionalProperties=false` とする
3. 入力の暗黙デフォルトは持たない（required で明示）
4. URL は `format=uri`、時刻は `format=date-time` を使う

### 6.1 `search`

目的:

- クエリから候補 URL を収集する

`timeout_seconds`: 20

`input_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["query", "top_k", "recency_days", "domains", "locale"],
  "properties": {
    "query": {
      "type": "string",
      "minLength": 1
    },
    "top_k": {
      "type": "integer",
      "minimum": 1,
      "maximum": 20
    },
    "recency_days": {
      "anyOf": [
        {
          "type": "integer",
          "minimum": 1
        },
        {
          "type": "null"
        }
      ]
    },
    "domains": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1
      }
    },
    "locale": {
      "type": "string",
      "pattern": "^[a-z]{2}-[A-Z]{2}$"
    }
  }
}
```

`result_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["items"],
  "properties": {
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["title", "url", "snippet", "published_at"],
        "properties": {
          "title": {
            "type": "string",
            "minLength": 1
          },
          "url": {
            "type": "string",
            "format": "uri"
          },
          "snippet": {
            "type": "string"
          },
          "published_at": {
            "anyOf": [
              {
                "type": "string",
                "format": "date-time"
              },
              {
                "type": "null"
              }
            ]
          }
        }
      }
    }
  }
}
```

`effect_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["effect_type", "query", "urls"],
  "properties": {
    "effect_type": {
      "const": "web.search_result"
    },
    "query": {
      "type": "string",
      "minLength": 1
    },
    "urls": {
      "type": "array",
      "items": {
        "type": "string",
        "format": "uri"
      }
    }
  }
}
```

### 6.2 `open_url`

目的:

- 指定 URL を取得して本文テキストを抽出する

`timeout_seconds`: 20

`input_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["url", "max_chars"],
  "properties": {
    "url": {
      "type": "string",
      "format": "uri"
    },
    "max_chars": {
      "type": "integer",
      "minimum": 1000,
      "maximum": 50000
    }
  }
}
```

`result_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["url", "title", "text", "fetched_at"],
  "properties": {
    "url": {
      "type": "string",
      "format": "uri"
    },
    "title": {
      "type": "string"
    },
    "text": {
      "type": "string"
    },
    "fetched_at": {
      "type": "string",
      "format": "date-time"
    }
  }
}
```

`effect_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["effect_type", "url", "text_digest"],
  "properties": {
    "effect_type": {
      "const": "web.page_opened"
    },
    "url": {
      "type": "string",
      "format": "uri"
    },
    "text_digest": {
      "type": "string",
      "minLength": 1
    }
  }
}
```

### 6.3 `extract_structured`

目的:

- 取得済みテキストから構造化情報を抽出する

`timeout_seconds`: 30

`input_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["source_url", "source_text", "target_schema_json"],
  "properties": {
    "source_url": {
      "type": "string",
      "format": "uri"
    },
    "source_text": {
      "type": "string",
      "minLength": 1
    },
    "target_schema_json": {
      "type": "object"
    }
  }
}
```

`result_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["source_url", "structured_data_json"],
  "properties": {
    "source_url": {
      "type": "string",
      "format": "uri"
    },
    "structured_data_json": {
      "type": "object"
    }
  }
}
```

`effect_schema_json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["effect_type", "source_url", "keys"],
  "properties": {
    "effect_type": {
      "const": "web.structured_extracted"
    },
    "source_url": {
      "type": "string",
      "format": "uri"
    },
    "keys": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1
      },
      "uniqueItems": true
    }
  }
}
```

## 7. Execute 規約

### 7.1 実行前

1. registry で `web_access + operation` を解決
2. input schema 検証失敗は `failed`（`execute_schema_invalid`）

### 7.2 実行中

1. adapter を1回だけ実行
2. timeout / HTTP / parse 失敗は `failed`（`execute_runtime_error`）

### 7.3 実行後

1. result schema 検証
2. effect schema 検証
3. 検証失敗は `failed`（`execute_schema_invalid`）

## 8. World Model 反映規則

### 8.1 `search` 後

1. URL を `wm_entities(entity_type="web_resource")` へ upsert
2. query と URL 群を `wm_links(link_type="evidence_for")` で記録

### 8.2 `open_url` 後

1. URL 観測を `wm_observations(source_type="action_result")` へ保存
2. `text_digest` から命題候補を `wm_beliefs` へ反映

### 8.3 `extract_structured` 後

1. 抽出キーを `wm_beliefs.value_json` へ反映
2. `source_url` 根拠を link/evidence に保持

ルール:

1. 矛盾命題は上書きせず並存
2. `confidence` は抽出確度に応じて設定

## 9. Event/WritePlan 連携

1. 実行結果は `events.source="autonomy_action"` で保存
2. 保存後に以下ジョブを投入
   - `upsert_event_embedding`
   - `upsert_event_assistant_summary`
   - `generate_write_plan`
3. ユーザー共有が必要な場合のみ `events/stream` 配信

## 10. Tacticalize 規約

1. `search` 単体 ticket は許可
2. `open_url` は URL 入手済みを precondition にする
3. `extract_structured` は source_text 入手済みを precondition にする
4. precondition 未成立 ticket は `cancelled`（`ticket_precondition_failed`）で確定

## 11. エラー規約

1. adapter 未登録: `execute_adapter_not_found`
2. schema 不一致: `execute_schema_invalid`
3. runtime 失敗: `execute_runtime_error`
4. 失敗時は `ActionResult.error_message` と `reason_code` を必須保存

## 12. 完了条件（Phase 5）

1. operation 3種が定義されている
2. input/result/effect schema が定義されている
3. `/api/chat` Web検索制御と分離されている
4. world model 反映規則が定義されている
5. Event/WritePlan 連携規則が定義されている
