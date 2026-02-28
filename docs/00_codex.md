# Codex 用: 作業開始インデックス

<!-- Block: Purpose -->
このファイルは **Codex が毎回の作業開始時に最初に読む**「索引」。
詳細な説明は重複させない（必要になったらリンク先を読む）。

<!-- Block: Always Read -->
## 毎回読む（最短で現在地に戻る）

- 全体初期化の流れ（設定DB→プリセット→記憶DB→ルータ登録）: `cocoro_ghost/main.py` の `create_app()` と `cocoro_ghost/app_bootstrap/config_bootstrap.py` / `cocoro_ghost/app_bootstrap/routers.py` / `cocoro_ghost/app_bootstrap/lifecycle.py`
- 依存生成（MemoryManager / ClockService / DB Depends）: `cocoro_ghost/app_bootstrap/dependencies.py`
- 同期/非同期の境界（SSE開始前に検索確定）: `docs/10_実行フロー.md`
- API（HTTPS必須/認証/主要エンドポイント）: `docs/07_API.md`
- 時刻基盤（system/domain二層）: `cocoro_ghost/core/clock.py` と `cocoro_ghost/api/control.py` / `cocoro_ghost/api/services/control_service.py`（`/api/control/time*`）
- 記憶処理の入口（mixin構成）: `cocoro_ghost/memory/manager.py`
- 非同期ジョブ（WritePlan/索引/整理など）: `cocoro_ghost/jobs/runner.py`（スケジューラ）/ `cocoro_ghost/jobs/registry.py`（ディスパッチ）/ `cocoro_ghost/jobs/handlers/*.py`（実処理）/ `cocoro_ghost/jobs/handlers/write_plan_generate.py` / `cocoro_ghost/jobs/handlers/write_plan_apply.py`（WritePlan分割）/ `cocoro_ghost/jobs/handlers/common.py`（共通ヘルパ）/ `cocoro_ghost/jobs/constants.py`（共通定数）/ `cocoro_ghost/jobs/internal_worker.py`
- 確定プロフィール（好み/苦手: ConfirmedPreferences）: `cocoro_ghost/memory/_chat_mixin.py` と `cocoro_ghost/jobs/handlers/write_plan.py`
- 保存先/パス（frozen/非frozen）: `cocoro_ghost/infra/paths.py`
- TOMLキー/検証（未知キーで起動失敗）: `cocoro_ghost/config.py` の `load_config()`

<!-- Block: Next Reads -->
## 作業タイプ別: 次に開く

- API 追加/変更: `cocoro_ghost/api/` と `cocoro_ghost/app_bootstrap/routers.py` と `docs/07_API.md`
- チャット（SSE）: `cocoro_ghost/api/chat.py` と `cocoro_ghost/memory/_chat_mixin.py`
- 自発行動/Capability設計: `docs/03_自発行動アーキテクチャ方針.md` と `cocoro_ghost/autonomy/orchestrator.py` / `cocoro_ghost/autonomy/repository.py` / `cocoro_ghost/autonomy/runtime_blackboard.py` / `cocoro_ghost/autonomy/policies/` / `cocoro_ghost/autonomy/capabilities/` / `cocoro_ghost/jobs/handlers/autonomy.py` / `cocoro_ghost/vision/desktop_watch.py` / `cocoro_ghost/vision/camera_watch.py` / `cocoro_ghost/reminders/service.py` / `cocoro_ghost/runtime/event_stream.py` / `cocoro_ghost/jobs/runner.py`
- 自発行動の詳細設計: `docs/18_自発行動アーキテクチャ詳細設計.md`（実装順序/データ/ジョブ/API/責務分割の実装契約）
- 自発行動の汎用エージェント委譲（実装前設計）: `docs/19_汎用エージェント委譲設計.md`（`agent_delegate` / `agent_jobs` / `agent_runner` / control API）
- 人格中心化（会話と自発行動の実装前設計）: `docs/20_人格中心化（会話と自発行動）実装設計.md`（`autonomy.message` の人格発話化 / 会話選別・Deliberation材料選別の人格化 / `current_thought_state` + `agenda_threads` 構想）
- 検索（思い出す）: `docs/04_検索（思い出す）.md` と `cocoro_ghost/memory/_chat_search_mixin.py`
- 記憶更新（育てる）: `docs/05_記憶更新（育てる）.md` と `cocoro_ghost/jobs/runner.py` / `cocoro_ghost/jobs/registry.py` / `cocoro_ghost/jobs/handlers/*.py`
- DB/ストレージ（SQLite）: `docs/06_ストレージ（SQLite）.md` と `cocoro_ghost/storage/db.py`
- Web UI: `docs/13_WebブラウザUI.md` と `static/` と `cocoro_ghost/app_bootstrap/routers.py`（static mount）
- 長期評価（会話/感情/時間前進）: `docs/15_長期会話評価計画.md` と `docs/16_長期会話シナリオ台帳.md`
- 感情境界 / heartbeat / 完了時共有判定: `docs/18_自発行動アーキテクチャ詳細設計.md`（自発行動の正本に統合済み）

<!-- Block: Search Cheatsheet -->
## 迷ったら `rg`（入口に当てる）

- ルータ/エンドポイント: `rg -n "APIRouter\\(|@router\\.|/api/" cocoro_ghost/api`
- 同期フローの本体: `rg -n "def stream_chat\\b|_chat_inflight_lock|SearchResultPack" cocoro_ghost/memory`
- 非同期ジョブ/キュー: `rg -n "\\bJob\\b|enqueue|run_forever|tidy_memory" cocoro_ghost`
- DBテーブル/モデル: `rg -n "class (Event|State|Job)\\b|retrieval_runs|event_links|state_links" cocoro_ghost`
- 好み/苦手（confirmed）: `rg -n "UserPreference|user_preferences|ConfirmedPreferences|preference_updates" cocoro_ghost docs`
- 設定の正（token/プリセット/active）: `rg -n "GlobalSettings|active_.*_preset_id|ensure_initial_settings" cocoro_ghost`

<!-- Block: Invariants -->
## 不変条件（踏み外し防止・短く）

- HTTPS 必須（自己署名）で運用する（`http://` では接続できない）: `docs/07_API.md`
- `/api/chat` は **SSE開始前に**「必要な記憶」を確定する: `docs/10_実行フロー.md`
- Web検索（インターネット）の**現行実装**は `/api/chat` の最終応答生成（L3）でのみ有効化可能（`llm_preset.reply_web_search_enabled`）: `docs/10_実行フロー.md` / `cocoro_ghost/llm/client.py`
- 自発行動向けWeb検索は `/api/chat` と**別経路**で実装する（`web_access` Capability、混在禁止）: `docs/03_自発行動アーキテクチャ方針.md` / `docs/18_自発行動アーキテクチャ詳細設計.md`
- token の正は `settings.db`（TOMLは初回の入口）: `docs/07_API.md`
- `settings.db` / `memory DB` は起動時に既知のスキーマ移行を行う（版ごとの実装は `cocoro_ghost/storage/db_migrations.py` を参照）: `cocoro_ghost/storage/db.py`
- `/api/chat` は単一ユーザー前提で **同時に1本**へ制限する: `cocoro_ghost/memory/_chat_mixin.py`
- `state` は `events` からの非同期更新（WritePlan）で育てる（直接入力しない）: `docs/03_自発行動アーキテクチャ方針.md` / `docs/05_記憶更新（育てる）.md`
- `config/setting.toml` は **未知キーを許可しない**（起動時に弾く）: `cocoro_ghost/config.py`

<!-- Block: Run/Build -->
## 起動/配布メモ（最小）

- 開発起動: `run.py`（uvicorn reload）
- 配布起動（PyInstaller）: `cocoro_ghost/entrypoint.py`
- TLS 証明書生成: `cocoro_ghost/infra/tls.py`
- 設定テンプレ: `config/setting.toml.release`（秘密は埋めない。例は `<TOKEN>` 形式にする）
- Windows 側で叩くコマンドは `.exe` を付ける（例: `python.exe`, `curl.exe`, `pyinstaller.exe`）

<!-- Block: Maintenance -->
## 更新ルール（重要）

- リポ構成/入口/責務分割/主要導線が変わったら、この `docs/00_codex.md` を更新する
- 詳細設計の読み順は `docs/00_はじめに.md` を参照（このファイルは“索引”に徹する）
