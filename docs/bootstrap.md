# 初期DB作成（bootstrap）

このドキュメントは、CocoroGhost の「初回起動時に自動で作られる DB」と、その作成順・責務境界をまとめる。
運用前のため、**マイグレーション**や**後方互換**は扱わない（存在しない前提）。

## 目的

- 起動に必要な前提（設定/DB/保存先フォルダ）を揃える
- 「どのDBに何が入るか」を明確にする
- 初期化が失敗する条件（例: vec0 次元不一致）を明確にする

## 生成されるDB

- 設定DB: `data/settings.db`
  - APIトークン（`global_settings.token`）
  - GlobalSettings（`memory_enabled` / `desktop_watch_*` / `active_*_preset_id`）
  - 各種プリセット（LLM / Embedding / Persona / Addon）
- リマインダーDB: `data/reminders.db`
  - リマインダーのグローバル設定（`reminders_enabled` / `target_client_id`）
  - リマインダー定義（once/daily/weekly）と実行状態（`next_fire_at_utc`）
- 記憶DB: `data/memory_<embedding_preset_id>.db`
  - Unit + payload_*（エピソード/ファクト/サマリ/ループ等）
  - jobs（非同期処理キュー）
  - FTS5（BM25用）とトリガー
  - sqlite-vec（vec0）仮想テーブル（`vec_units`）

## 初期化の流れ（起動時）

実装上の起動順（概略）は次の通り。

1. `config/setting.toml` を読む
   - 少なくとも `token` / `log_level` 等を読み取る
2. `settings.db` を初期化する（無ければ作成）
3. `reminders.db` を初期化する（無ければ作成）
4. `settings.db` に初期レコードを投入する（空なら）
   - `global_settings`（単一行）
   - `llm_presets` / `embedding_presets` / `persona_presets` / `addon_presets`（デフォルト）
   - `active_*_preset_id` の設定
   - `global_settings.token` が空なら TOML の `token` を投入する（DBを正とするため）
5. `reminders.db` に初期レコードを投入する（空なら）
   - `reminder_global_settings`（単一行）
6. アクティブなプリセットを読み込み、RuntimeConfig を構築する
7. `memory_<active_embedding_preset_id>.db` を初期化する
   - vec0 次元（`embedding_dimension`）が既存DBと不一致ならエラー（別 `embedding_preset_id` を使うか、DBを再構築する）
   - FTS5 と同期トリガーを用意する

補足:
- 記憶DBは `/api/chat` の `embedding_preset_id` で **per-request** に開けるが、内蔵Workerは `active_embedding_preset_id` のDBのみを処理する（`docs/worker.md` / `docs/api.md`）。

## 失敗しやすいポイント

- **vec0 次元不一致**:
  - 同じ `memory_<embedding_preset_id>.db` を使い回したまま `embedding_dimension` を変えると初期化で失敗する
  - 対応: 別 `embedding_preset_id`（=別DB）を作るか、DBを再構築する

