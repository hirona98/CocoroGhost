# cocoro_ghost 設計書（パートナー最適 / sqlite-vec固定）

この設計は、`cocoro_ghost` を「AIパートナー」として最適化するための新仕様です。詳細設計は `docs/partner_spec/` 以下に分割しています。

## 目的

- 人格の一貫性（PersonaAnchor）
- 関係性の連続性（RelationshipContract / SharedNarrative）
- 会話テンポ（同期は軽く、重い処理はWorkerへ）
- 長期運用での“育ち”（統合・整理・矛盾管理）

## 前提（固定）

- LLM/Embedding: API経由（LiteLLMで切替可能）
- ベクター検索: sqlite-vec（vec0）固定
- ストレージ: `settings.db` + `memory_<memory_id>.db`
- “Unit（共通メタ）＋Payload（種別本文）” に統一

## 新仕様ドキュメント（入口）

- 全体像: `docs/partner_spec/README.md`
- アーキテクチャ: `docs/partner_spec/architecture.md`
- 設定DB: `docs/partner_spec/settings_db.md`
- DBスキーマ/Enum: `docs/partner_spec/db_schema.md`
- sqlite-vec設計: `docs/partner_spec/sqlite_vec.md`
- Scheduler仕様: `docs/partner_spec/scheduler.md`
- Worker仕様: `docs/partner_spec/worker.md`
- LLM JSONスキーマ: `docs/partner_spec/prompts.md`
- API仕様: `docs/partner_spec/api.md`
- 初期化: `docs/partner_spec/bootstrap.md`
- テスト/DoD: `docs/partner_spec/testing.md`
