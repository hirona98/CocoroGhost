"""
DB マイグレーション処理（設定DB / 記憶DB）。

役割:
- db.py から呼び出される SQLite user_version ベースの段階的マイグレーションを提供する。
- 起動時に必要な版だけを順送りで更新する。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from cocoro_ghost.autonomy.action_keys import canonical_action_key


def _get_sqlite_user_version(conn) -> int:
    """SQLite PRAGMA user_version を整数で取得する。"""

    # --- SQLite の schema version 識別値を読む ---
    row = conn.execute(text("PRAGMA user_version")).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _get_table_columns(conn, table_name: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""

    # --- PRAGMA table_info で列一覧を取得する ---
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {str(r[1]) for r in rows}


def _migrate_settings_db_v2_to_v3(engine, logger: logging.Logger) -> None:
    """設定DBを v2 から v3 へ移行する。"""

    with engine.connect() as conn:
        # --- v2 では llm_presets が存在する前提 ---
        llm_table_exists = (
            conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_presets'")).fetchone()
            is not None
        )
        if not llm_table_exists:
            raise RuntimeError(
                "settings DB migration failed: llm_presets table missing in user_version=2 database."
            )

        # --- 最終応答Web検索フラグ列を追加（既存行は既定値1） ---
        columns = _get_table_columns(conn, "llm_presets")
        if "reply_web_search_enabled" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE llm_presets "
                    "ADD COLUMN reply_web_search_enabled INTEGER NOT NULL DEFAULT 1"
                )
            )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=3"))
        conn.commit()

    logger.info("settings DB migrated: user_version 2 -> 3")


def _migrate_settings_db_v3_to_v4(engine, logger: logging.Logger) -> None:
    """設定DBを v3 から v4 へ移行する。"""

    with engine.connect() as conn:
        # --- global_settings へ autonomy/camera 設定列を追加 ---
        gs_columns = _get_table_columns(conn, "global_settings")
        if "autonomy_enabled" not in gs_columns:
            conn.execute(text("ALTER TABLE global_settings ADD COLUMN autonomy_enabled INTEGER NOT NULL DEFAULT 0"))
        if "autonomy_heartbeat_seconds" not in gs_columns:
            conn.execute(
                text(
                    "ALTER TABLE global_settings "
                    "ADD COLUMN autonomy_heartbeat_seconds INTEGER NOT NULL DEFAULT 30"
                )
            )
        if "autonomy_max_parallel_intents" not in gs_columns:
            conn.execute(
                text(
                    "ALTER TABLE global_settings "
                    "ADD COLUMN autonomy_max_parallel_intents INTEGER NOT NULL DEFAULT 2"
                )
            )
        if "camera_watch_enabled" not in gs_columns:
            conn.execute(text("ALTER TABLE global_settings ADD COLUMN camera_watch_enabled INTEGER NOT NULL DEFAULT 0"))
        if "camera_watch_interval_seconds" not in gs_columns:
            conn.execute(
                text(
                    "ALTER TABLE global_settings "
                    "ADD COLUMN camera_watch_interval_seconds INTEGER NOT NULL DEFAULT 15"
                )
            )

        # --- llm_presets へ旧 Deliberation 設定列を追加（旧v4仕様） ---
        llm_columns = _get_table_columns(conn, "llm_presets")
        if "deliberation_model" not in llm_columns:
            conn.execute(
                text(
                    "ALTER TABLE llm_presets "
                    "ADD COLUMN deliberation_model TEXT NOT NULL DEFAULT 'openai/gpt-5-mini'"
                )
            )
        if "deliberation_max_tokens" not in llm_columns:
            conn.execute(
                text(
                    "ALTER TABLE llm_presets "
                    "ADD COLUMN deliberation_max_tokens INTEGER NOT NULL DEFAULT 4096"
                )
            )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=4"))
        conn.commit()

    logger.info("settings DB migrated: user_version 3 -> 4")


def _migrate_settings_db_v4_to_v5(engine, logger: logging.Logger) -> None:
    """設定DBを v4 から v5 へ移行する。"""

    with engine.connect() as conn:
        # --- v5 は deliberation_* 廃止後の版 ---
        # --- 旧カラムが残っていても ORM/API は使用しないため、ここでは版更新のみ行う ---
        conn.execute(text("PRAGMA user_version=5"))
        conn.commit()

    logger.info("settings DB migrated: user_version 4 -> 5")


def _migrate_settings_db_v5_to_v6(engine, logger: logging.Logger) -> None:
    """設定DBを v5 から v6 へ移行する。"""

    with engine.connect() as conn:
        # --- global_settings に gmini backend 実行コマンド列を追加（旧命名） ---
        gs_columns = _get_table_columns(conn, "global_settings")
        if "agent_backend_gmini_command" not in gs_columns:
            conn.execute(
                text(
                    "ALTER TABLE global_settings "
                    "ADD COLUMN agent_backend_gmini_command TEXT NOT NULL DEFAULT 'gemini.exe -p'"
                )
            )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=6"))
        conn.commit()

    logger.info("settings DB migrated: user_version 5 -> 6")


def _migrate_settings_db_v6_to_v7(engine, logger: logging.Logger) -> None:
    """設定DBを v6 から v7 へ移行する。"""

    with engine.connect() as conn:
        # --- global_settings に cli_agent backend のCLIコマンド列を追加 ---
        gs_columns = _get_table_columns(conn, "global_settings")
        if "agent_backend_cli_agent_command" not in gs_columns:
            conn.execute(
                text(
                    "ALTER TABLE global_settings "
                    "ADD COLUMN agent_backend_cli_agent_command TEXT NOT NULL DEFAULT 'gemini.exe -p'"
                )
            )

        # --- 旧 gmini 列があり、新列が空のときは値を引き継ぐ ---
        gs_columns_after = _get_table_columns(conn, "global_settings")
        if "agent_backend_gmini_command" in gs_columns_after and "agent_backend_cli_agent_command" in gs_columns_after:
            conn.execute(
                text(
                    "UPDATE global_settings "
                    "SET agent_backend_cli_agent_command = agent_backend_gmini_command "
                    "WHERE length(trim(COALESCE(agent_backend_cli_agent_command,''))) = 0 "
                    "AND length(trim(COALESCE(agent_backend_gmini_command,''))) > 0"
                )
            )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=7"))
        conn.commit()

    logger.info("settings DB migrated: user_version 6 -> 7")


def migrate_settings_db_if_needed(*, engine, target_user_version: int, logger: logging.Logger) -> None:
    """設定DBに必要なマイグレーションを適用する。"""

    # --- 既知版を順に進める（飛び番はここでは扱わない） ---
    while True:
        with engine.connect() as conn:
            current = _get_sqlite_user_version(conn)

        if current == 2 and int(target_user_version) >= 3:
            _migrate_settings_db_v2_to_v3(engine, logger)
            continue
        if current == 3 and int(target_user_version) >= 4:
            _migrate_settings_db_v3_to_v4(engine, logger)
            continue
        if current == 4 and int(target_user_version) >= 5:
            _migrate_settings_db_v4_to_v5(engine, logger)
            continue
        if current == 5 and int(target_user_version) >= 6:
            _migrate_settings_db_v5_to_v6(engine, logger)
            continue
        if current == 6 and int(target_user_version) >= 7:
            _migrate_settings_db_v6_to_v7(engine, logger)
            continue
        break


def _migrate_memory_db_v10_to_v11(engine, logger: logging.Logger) -> None:
    """記憶DBを v10 から v11 へ移行する（agent_jobs 追加）。"""

    with engine.connect() as conn:
        # --- agent_jobs テーブルを追加（既存ならそのまま） ---
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    job_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    task_instruction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    claim_token TEXT,
                    runner_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    heartbeat_at INTEGER,
                    result_status TEXT,
                    result_summary_text TEXT,
                    result_details_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT,
                    error_message TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    CONSTRAINT pk_agent_jobs PRIMARY KEY (job_id),
                    CONSTRAINT uq_agent_jobs_intent_id UNIQUE (intent_id),
                    CONSTRAINT ck_agent_jobs_status CHECK (
                        status IN ('queued','claimed','running','completed','failed','cancelled','timed_out')
                    ),
                    CONSTRAINT ck_agent_jobs_failed_error_message CHECK (
                        status <> 'failed' OR length(trim(COALESCE(error_message,''))) > 0
                    ),
                    CONSTRAINT ck_agent_jobs_result_status CHECK (
                        result_status IS NULL OR result_status IN ('success','partial','failed','no_effect')
                    ),
                    CONSTRAINT ck_agent_jobs_backend_non_empty CHECK (length(trim(backend)) > 0),
                    CONSTRAINT ck_agent_jobs_task_instruction_non_empty CHECK (length(trim(task_instruction)) > 0),
                    CONSTRAINT fk_agent_jobs_intent_id FOREIGN KEY (intent_id) REFERENCES intents(intent_id) ON DELETE CASCADE,
                    CONSTRAINT fk_agent_jobs_decision_id FOREIGN KEY (decision_id) REFERENCES action_decisions(decision_id) ON DELETE CASCADE
                )
                """
            )
        )

        # --- 参照/監視用インデックスを追加 ---
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_created_at ON agent_jobs(status, created_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_jobs_backend_status ON agent_jobs(backend, status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_agent_jobs_heartbeat_at ON agent_jobs(heartbeat_at)"))

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=11"))
        conn.commit()

    logger.info("memory DB migrated: user_version 10 -> 11")


def _migrate_memory_db_v11_to_v12(engine, logger: logging.Logger) -> None:
    """記憶DBを v11 から v12 へ移行する（action_decisions.console_delivery_json 追加）。"""

    default_console_delivery_json = (
        '{"on_complete":"activity_only","on_fail":"activity_only","on_progress":"activity_only","message_kind":"report"}'
    )

    with engine.connect() as conn:
        # --- action_decisions に Console 表示方針列を追加（既存行は既定値で埋める） ---
        columns = _get_table_columns(conn, "action_decisions")
        if "console_delivery_json" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE action_decisions "
                    "ADD COLUMN console_delivery_json TEXT NOT NULL "
                    "DEFAULT '{\"on_complete\":\"activity_only\",\"on_fail\":\"activity_only\","
                    "\"on_progress\":\"activity_only\",\"message_kind\":\"report\"}'"
                )
            )

        # --- 既存データの空値を正規既定値へ補正する ---
        conn.execute(
            text(
                "UPDATE action_decisions "
                "SET console_delivery_json = :default_json "
                "WHERE length(trim(COALESCE(console_delivery_json,''))) = 0"
            ),
            {"default_json": str(default_console_delivery_json)},
        )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=12"))
        conn.commit()

    logger.info("memory DB migrated: user_version 11 -> 12")


def _migrate_memory_db_v12_to_v13(engine, logger: logging.Logger) -> None:
    """記憶DBを v12 から v13 へ移行する（agenda_threads 追加 + current_thought_state へ改名）。"""

    with engine.connect() as conn:
        # --- agenda_threads テーブルを追加（現在の思考の実体） ---
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agenda_threads (
                    thread_id TEXT NOT NULL,
                    thread_key TEXT NOT NULL,
                    source_event_id INTEGER,
                    source_result_id TEXT,
                    kind TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    goal TEXT,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    next_action_type TEXT,
                    next_action_payload_json TEXT NOT NULL DEFAULT '{}',
                    followup_due_at INTEGER,
                    last_progress_at INTEGER,
                    last_result_status TEXT,
                    talk_impulse_level TEXT NOT NULL DEFAULT 'none',
                    talk_impulse_reason TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    CONSTRAINT pk_agenda_threads PRIMARY KEY (thread_id),
                    CONSTRAINT uq_agenda_threads_thread_key UNIQUE (thread_key),
                    CONSTRAINT ck_agenda_threads_status CHECK (
                        status IN ('open','active','blocked','satisfied','stale','closed')
                    ),
                    CONSTRAINT ck_agenda_threads_talk_impulse_level CHECK (
                        talk_impulse_level IN ('none','low','high','speak_now')
                    ),
                    CONSTRAINT ck_agenda_threads_kind_non_empty CHECK (length(trim(kind)) > 0),
                    CONSTRAINT ck_agenda_threads_topic_non_empty CHECK (length(trim(topic)) > 0),
                    CONSTRAINT fk_agenda_threads_source_event_id FOREIGN KEY (source_event_id) REFERENCES events(event_id) ON DELETE SET NULL,
                    CONSTRAINT fk_agenda_threads_source_result_id FOREIGN KEY (source_result_id) REFERENCES action_results(result_id) ON DELETE SET NULL
                )
                """
            )
        )

        # --- 参照/スケジューリング用インデックスを追加 ---
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_status_followup_due_at "
                "ON agenda_threads(status, followup_due_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_updated_at "
                "ON agenda_threads(updated_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_talk_impulse_level "
                "ON agenda_threads(talk_impulse_level)"
            )
        )

        # --- 現在の思考 state を新名称へ改名する ---
        conn.execute(
            text(
                "UPDATE state "
                "SET kind = 'current_thought_state' "
                "WHERE kind = 'persona_interest_state'"
            )
        )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=13"))
        conn.commit()

    logger.info("memory DB migrated: user_version 12 -> 13")


def _migrate_memory_db_v13_to_v14(engine, logger: logging.Logger) -> None:
    """記憶DBを v13 から v14 へ移行する（action_decisions.agenda_thread_id 追加）。"""

    with engine.connect() as conn:
        # --- action_decisions に対象 agenda thread 列を追加する ---
        columns = _get_table_columns(conn, "action_decisions")
        if "agenda_thread_id" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE action_decisions "
                    "ADD COLUMN agenda_thread_id TEXT"
                )
            )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=14"))
        conn.commit()

    logger.info("memory DB migrated: user_version 13 -> 14")


def _migrate_memory_db_v14_to_v15(engine, logger: logging.Logger) -> None:
    """記憶DBを v14 から v15 へ移行する（agenda_threads の発話衝動列へ改名）。"""

    with engine.connect() as conn:
        def _map_talk_impulse_level(raw: object) -> str:
            text_value = str(raw or "").strip()
            if text_value == "mention":
                return "low"
            if text_value == "notify":
                return "high"
            if text_value == "chat":
                return "speak_now"
            if text_value in {"low", "high", "speak_now"}:
                return text_value
            if text_value in {"", "none"}:
                return "none"
            raise RuntimeError(f"unsupported talk impulse legacy level: {text_value}")

        columns = _get_table_columns(conn, "agenda_threads")

        # --- 旧 agenda_threads を新スキーマへ作り直す ---
        if "talk_impulse_level" not in columns:
            conn.execute(text("ALTER TABLE agenda_threads RENAME TO agenda_threads_v14_legacy"))
            conn.execute(
                text(
                    """
                    CREATE TABLE agenda_threads (
                        thread_id TEXT NOT NULL,
                        thread_key TEXT NOT NULL,
                        source_event_id INTEGER,
                        source_result_id TEXT,
                        kind TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        goal TEXT,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 50,
                        next_action_type TEXT,
                        next_action_payload_json TEXT NOT NULL DEFAULT '{}',
                        followup_due_at INTEGER,
                        last_progress_at INTEGER,
                        last_result_status TEXT,
                        talk_impulse_level TEXT NOT NULL DEFAULT 'none',
                        talk_impulse_reason TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        CONSTRAINT pk_agenda_threads PRIMARY KEY (thread_id),
                        CONSTRAINT uq_agenda_threads_thread_key UNIQUE (thread_key),
                        CONSTRAINT ck_agenda_threads_status CHECK (
                            status IN ('open','active','blocked','satisfied','stale','closed')
                        ),
                        CONSTRAINT ck_agenda_threads_talk_impulse_level CHECK (
                            talk_impulse_level IN ('none','low','high','speak_now')
                        ),
                        CONSTRAINT ck_agenda_threads_kind_non_empty CHECK (length(trim(kind)) > 0),
                        CONSTRAINT ck_agenda_threads_topic_non_empty CHECK (length(trim(topic)) > 0),
                        CONSTRAINT fk_agenda_threads_source_event_id FOREIGN KEY (source_event_id) REFERENCES events(event_id) ON DELETE SET NULL,
                        CONSTRAINT fk_agenda_threads_source_result_id FOREIGN KEY (source_result_id) REFERENCES action_results(result_id) ON DELETE SET NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO agenda_threads (
                        thread_id,
                        thread_key,
                        source_event_id,
                        source_result_id,
                        kind,
                        topic,
                        goal,
                        status,
                        priority,
                        next_action_type,
                        next_action_payload_json,
                        followup_due_at,
                        last_progress_at,
                        last_result_status,
                        talk_impulse_level,
                        talk_impulse_reason,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    SELECT
                        thread_id,
                        thread_key,
                        source_event_id,
                        source_result_id,
                        kind,
                        topic,
                        goal,
                        status,
                        priority,
                        next_action_type,
                        next_action_payload_json,
                        followup_due_at,
                        last_progress_at,
                        last_result_status,
                        CASE
                            WHEN COALESCE(last_result_status, '') = 'failed' THEN 'speak_now'
                            WHEN COALESCE(report_candidate_level, '') = 'mention' THEN 'low'
                            WHEN COALESCE(report_candidate_level, '') = 'notify' THEN 'high'
                            WHEN COALESCE(report_candidate_level, '') = 'chat' THEN 'speak_now'
                            ELSE 'none'
                        END,
                        report_candidate_reason,
                        metadata_json,
                        created_at,
                        updated_at
                    FROM agenda_threads_v14_legacy
                    """
                )
            )
            conn.execute(text("DROP TABLE agenda_threads_v14_legacy"))

        # --- 旧 console_delivery 値を新列挙へ正規化する ---
        decision_rows = conn.execute(
            text(
                "SELECT decision_id, console_delivery_json "
                "FROM action_decisions "
                "WHERE length(trim(COALESCE(console_delivery_json,''))) > 0"
            )
        ).fetchall()
        for decision_id, console_delivery_json in decision_rows:
            try:
                payload_obj = json.loads(str(console_delivery_json))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"memory DB migration failed: action_decisions.console_delivery_json is invalid JSON "
                    f"(decision_id={decision_id})."
                ) from exc
            if not isinstance(payload_obj, dict):
                raise RuntimeError(
                    f"memory DB migration failed: action_decisions.console_delivery_json is not an object "
                    f"(decision_id={decision_id})."
                )

            changed = False
            for key in ("on_complete", "on_fail"):
                if str(payload_obj.get(key) or "").strip() == "notify":
                    payload_obj[key] = "chat"
                    changed = True
            if not changed:
                continue

            conn.execute(
                text(
                    "UPDATE action_decisions "
                    "SET console_delivery_json = :payload_json "
                    "WHERE decision_id = :decision_id"
                ),
                {
                    "payload_json": json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")),
                    "decision_id": str(decision_id),
                },
            )

        # --- current_thought_state の旧 payload キー/値も新名称へ寄せる ---
        state_rows = conn.execute(
            text(
                "SELECT state_id, payload_json "
                "FROM state "
                "WHERE kind = 'current_thought_state' "
                "AND length(trim(COALESCE(payload_json,''))) > 0"
            )
        ).fetchall()
        for state_id, payload_json in state_rows:
            try:
                payload_obj = json.loads(str(payload_json))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"memory DB migration failed: current_thought_state.payload_json is invalid JSON "
                    f"(state_id={state_id})."
                ) from exc
            if not isinstance(payload_obj, dict):
                raise RuntimeError(
                    f"memory DB migration failed: current_thought_state.payload_json is not an object "
                    f"(state_id={state_id})."
                )

            changed = False
            if "talk_candidate" in payload_obj and "talk_impulse" not in payload_obj:
                talk_candidate_obj = payload_obj.pop("talk_candidate")
                if not isinstance(talk_candidate_obj, dict):
                    raise RuntimeError(
                        f"memory DB migration failed: current_thought_state.talk_candidate is not an object "
                        f"(state_id={state_id})."
                    )
                talk_impulse_level = _map_talk_impulse_level(talk_candidate_obj.get("level"))
                updated_from_obj = payload_obj.get("updated_from")
                if isinstance(updated_from_obj, dict) and str(updated_from_obj.get("result_status") or "").strip() == "failed":
                    talk_impulse_level = "speak_now"
                payload_obj["talk_impulse"] = {
                    "level": talk_impulse_level,
                    "reason": str(talk_candidate_obj.get("reason") or "").strip() or None,
                }
                changed = True

            agenda_threads = payload_obj.get("agenda_threads")
            if isinstance(agenda_threads, list):
                new_rows: list[object] = []
                row_changed = False
                for row in agenda_threads:
                    if not isinstance(row, dict):
                        raise RuntimeError(
                            f"memory DB migration failed: current_thought_state.agenda_threads item is not an object "
                            f"(state_id={state_id})."
                        )
                    row_obj = dict(row)
                    if "report_candidate_level" in row_obj and "talk_impulse_level" not in row_obj:
                        row_talk_impulse_level = _map_talk_impulse_level(row_obj.pop("report_candidate_level"))
                        if str(row_obj.get("status") or "").strip() == "blocked":
                            row_talk_impulse_level = "speak_now"
                        row_obj["talk_impulse_level"] = row_talk_impulse_level
                        row_changed = True
                    if "report_candidate_reason" in row_obj and "talk_impulse_reason" not in row_obj:
                        row_obj["talk_impulse_reason"] = row_obj.pop("report_candidate_reason")
                        row_changed = True
                    new_rows.append(row_obj)
                if row_changed:
                    payload_obj["agenda_threads"] = new_rows
                    changed = True

            if not changed:
                continue

            conn.execute(
                text(
                    "UPDATE state "
                    "SET payload_json = :payload_json "
                    "WHERE state_id = :state_id"
                ),
                {
                    "payload_json": json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")),
                    "state_id": int(state_id),
                },
            )

        # --- 旧インデックス名が残っていても新名だけを正本にする ---
        conn.execute(text("DROP INDEX IF EXISTS idx_agenda_threads_report_candidate_level"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_status_followup_due_at "
                "ON agenda_threads(status, followup_due_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_updated_at "
                "ON agenda_threads(updated_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_talk_impulse_level "
                "ON agenda_threads(talk_impulse_level)"
            )
        )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=15"))
        conn.commit()

    logger.info("memory DB migrated: user_version 14 -> 15")


def _migrate_memory_db_v15_to_v16(engine, logger: logging.Logger) -> None:
    """記憶DBを v15 から v16 へ移行する（完了時発話契約へ統合）。"""

    with engine.connect() as conn:
        def _map_progress_mode(raw: object) -> str:
            text_value = str(raw or "").strip()
            if text_value in {"silent", "activity_only"}:
                return text_value
            raise RuntimeError(f"unsupported progress visibility: {text_value}")

        def _map_completion_policy(*, on_complete_raw: object, on_fail_raw: object) -> str:
            on_complete_value = str(on_complete_raw or "").strip()
            on_fail_value = str(on_fail_raw or "").strip()

            # --- すでに新契約なら、そのまま通す ---
            if on_complete_value in {"silent", "on_error", "on_material", "always"}:
                return on_complete_value

            # --- 旧契約を、完了時の発話契約へ縮約する ---
            complete_requests_speech = on_complete_value in {"chat", "notify"}
            fail_requests_speech = on_fail_value in {"chat", "notify"}

            if complete_requests_speech:
                return "on_material"
            if fail_requests_speech:
                return "on_error"
            if on_complete_value in {"", "silent", "activity_only"} and on_fail_value in {"", "silent", "activity_only"}:
                return "silent"

            raise RuntimeError(
                f"unsupported completion speech legacy values: on_complete={on_complete_value!r}, on_fail={on_fail_value!r}"
            )

        # --- action_decisions.console_delivery_json を新契約へ正規化する ---
        decision_rows = conn.execute(
            text(
                "SELECT decision_id, console_delivery_json "
                "FROM action_decisions "
                "WHERE length(trim(COALESCE(console_delivery_json,''))) > 0"
            )
        ).fetchall()
        for decision_id, console_delivery_json in decision_rows:
            try:
                payload_obj = json.loads(str(console_delivery_json))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"memory DB migration failed: action_decisions.console_delivery_json is invalid JSON "
                    f"(decision_id={decision_id})."
                ) from exc
            if not isinstance(payload_obj, dict):
                raise RuntimeError(
                    f"memory DB migration failed: action_decisions.console_delivery_json is not an object "
                    f"(decision_id={decision_id})."
                )

            normalized_payload = {
                "on_complete": _map_completion_policy(
                    on_complete_raw=payload_obj.get("on_complete"),
                    on_fail_raw=payload_obj.get("on_fail"),
                ),
                "on_progress": _map_progress_mode(payload_obj.get("on_progress")),
            }

            if payload_obj == normalized_payload:
                continue

            conn.execute(
                text(
                    "UPDATE action_decisions "
                    "SET console_delivery_json = :payload_json "
                    "WHERE decision_id = :decision_id"
                ),
                {
                    "payload_json": json.dumps(normalized_payload, ensure_ascii=False, separators=(",", ":")),
                    "decision_id": str(decision_id),
                },
            )

        # --- agenda_threads から旧発話衝動列を削除する ---
        columns = _get_table_columns(conn, "agenda_threads")
        if "talk_impulse_level" in columns or "talk_impulse_reason" in columns:
            conn.execute(text("ALTER TABLE agenda_threads RENAME TO agenda_threads_v15_legacy"))
            conn.execute(
                text(
                    """
                    CREATE TABLE agenda_threads (
                        thread_id TEXT NOT NULL,
                        thread_key TEXT NOT NULL,
                        source_event_id INTEGER,
                        source_result_id TEXT,
                        kind TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        goal TEXT,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 50,
                        next_action_type TEXT,
                        next_action_payload_json TEXT NOT NULL DEFAULT '{}',
                        followup_due_at INTEGER,
                        last_progress_at INTEGER,
                        last_result_status TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        CONSTRAINT pk_agenda_threads PRIMARY KEY (thread_id),
                        CONSTRAINT uq_agenda_threads_thread_key UNIQUE (thread_key),
                        CONSTRAINT ck_agenda_threads_status CHECK (
                            status IN ('open','active','blocked','satisfied','stale','closed')
                        ),
                        CONSTRAINT ck_agenda_threads_kind_non_empty CHECK (length(trim(kind)) > 0),
                        CONSTRAINT ck_agenda_threads_topic_non_empty CHECK (length(trim(topic)) > 0),
                        CONSTRAINT fk_agenda_threads_source_event_id FOREIGN KEY (source_event_id) REFERENCES events(event_id) ON DELETE SET NULL,
                        CONSTRAINT fk_agenda_threads_source_result_id FOREIGN KEY (source_result_id) REFERENCES action_results(result_id) ON DELETE SET NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO agenda_threads (
                        thread_id,
                        thread_key,
                        source_event_id,
                        source_result_id,
                        kind,
                        topic,
                        goal,
                        status,
                        priority,
                        next_action_type,
                        next_action_payload_json,
                        followup_due_at,
                        last_progress_at,
                        last_result_status,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    SELECT
                        thread_id,
                        thread_key,
                        source_event_id,
                        source_result_id,
                        kind,
                        topic,
                        goal,
                        status,
                        priority,
                        next_action_type,
                        next_action_payload_json,
                        followup_due_at,
                        last_progress_at,
                        last_result_status,
                        metadata_json,
                        created_at,
                        updated_at
                    FROM agenda_threads_v15_legacy
                    """
                )
            )
            conn.execute(text("DROP TABLE agenda_threads_v15_legacy"))

        # --- current_thought_state payload から旧発話関連キーを除去する ---
        state_rows = conn.execute(
            text(
                "SELECT state_id, payload_json "
                "FROM state "
                "WHERE kind = 'current_thought_state' "
                "AND length(trim(COALESCE(payload_json,''))) > 0"
            )
        ).fetchall()
        for state_id, payload_json in state_rows:
            try:
                payload_obj = json.loads(str(payload_json))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"memory DB migration failed: current_thought_state.payload_json is invalid JSON "
                    f"(state_id={state_id})."
                ) from exc
            if not isinstance(payload_obj, dict):
                raise RuntimeError(
                    f"memory DB migration failed: current_thought_state.payload_json is not an object "
                    f"(state_id={state_id})."
                )

            changed = False
            for key in ("talk_candidate", "talk_impulse"):
                if key in payload_obj:
                    payload_obj.pop(key, None)
                    changed = True

            agenda_threads = payload_obj.get("agenda_threads")
            if isinstance(agenda_threads, list):
                new_rows: list[object] = []
                row_changed = False
                for row in agenda_threads:
                    if not isinstance(row, dict):
                        raise RuntimeError(
                            f"memory DB migration failed: current_thought_state.agenda_threads item is not an object "
                            f"(state_id={state_id})."
                        )
                    row_obj = dict(row)
                    for key in (
                        "report_candidate_level",
                        "report_candidate_reason",
                        "talk_impulse_level",
                        "talk_impulse_reason",
                        "delivery_mode",
                    ):
                        if key in row_obj:
                            row_obj.pop(key, None)
                            row_changed = True
                    new_rows.append(row_obj)
                if row_changed:
                    payload_obj["agenda_threads"] = new_rows
                    changed = True

            if not changed:
                continue

            conn.execute(
                text(
                    "UPDATE state "
                    "SET payload_json = :payload_json "
                    "WHERE state_id = :state_id"
                ),
                {
                    "payload_json": json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")),
                    "state_id": int(state_id),
                },
            )

        # --- 旧インデックスは削除し、新正本だけを残す ---
        conn.execute(text("DROP INDEX IF EXISTS idx_agenda_threads_talk_impulse_level"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_status_followup_due_at "
                "ON agenda_threads(status, followup_due_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_updated_at "
                "ON agenda_threads(updated_at)"
            )
        )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=16"))
        conn.commit()

    logger.info("memory DB migrated: user_version 15 -> 16")


def _migrate_memory_db_v16_to_v17(engine, logger: logging.Logger) -> None:
    """記憶DBを v16 から v17 へ移行する（active intent の同一 action key を一意化）。"""

    with engine.connect() as conn:
        # --- intents に canonical_action_key 列を追加する ---
        intent_columns = _get_table_columns(conn, "intents")
        if "canonical_action_key" not in intent_columns:
            conn.execute(
                text(
                    "ALTER TABLE intents "
                    "ADD COLUMN canonical_action_key TEXT NOT NULL DEFAULT ''"
                )
            )

        # --- 既存 intents の canonical_action_key を再計算する ---
        intent_rows = conn.execute(
            text(
                """
                SELECT
                    intent_id,
                    action_type,
                    action_payload_json,
                    status,
                    created_at,
                    updated_at
                  FROM intents
                """
            )
        ).fetchall()

        active_rows_by_key: dict[str, list[dict[str, object]]] = {}
        active_status_rank = {"running": 0, "blocked": 1, "queued": 2}

        for row in intent_rows:
            intent_id = str(row[0] or "").strip()
            if not intent_id:
                continue

            # --- payload JSON は構造比較の正本なので、壊れていれば移行を止める ---
            try:
                action_payload_obj = json.loads(str(row[2] or "{}"))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"memory DB migration failed: intents.action_payload_json is invalid JSON "
                    f"(intent_id={intent_id})."
                ) from exc
            if not isinstance(action_payload_obj, dict):
                raise RuntimeError(
                    f"memory DB migration failed: intents.action_payload_json is not an object "
                    f"(intent_id={intent_id})."
                )

            # --- 全 intent に canonical_action_key を保存する ---
            key = canonical_action_key(
                action_type=str(row[1] or ""),
                action_payload=dict(action_payload_obj),
            )
            conn.execute(
                text(
                    "UPDATE intents "
                    "SET canonical_action_key = :canonical_action_key "
                    "WHERE intent_id = :intent_id"
                ),
                {
                    "canonical_action_key": str(key),
                    "intent_id": str(intent_id),
                },
            )

            # --- active intent の重複だけを移行時に整理する ---
            status = str(row[3] or "").strip()
            if status not in {"queued", "running", "blocked"}:
                continue
            if not key:
                continue

            bucket = active_rows_by_key.get(str(key))
            if bucket is None:
                bucket = []
                active_rows_by_key[str(key)] = bucket
            bucket.append(
                {
                    "intent_id": str(intent_id),
                    "status": str(status),
                    "created_at": int(row[4] or 0),
                    "updated_at": int(row[5] or 0),
                }
            )

        # --- 重複 active intent は1件だけ残し、他は dropped へ落とす ---
        for key, rows in active_rows_by_key.items():
            ordered_rows = sorted(
                list(rows),
                key=lambda item: (
                    int(active_status_rank.get(str(item.get("status") or ""), 9)),
                    int(item.get("created_at") or 0),
                    int(item.get("updated_at") or 0),
                    str(item.get("intent_id") or ""),
                ),
            )
            for duplicate_row in ordered_rows[1:]:
                drop_ts = max(
                    int(duplicate_row.get("updated_at") or 0),
                    int(duplicate_row.get("created_at") or 0),
                    1,
                )
                conn.execute(
                    text(
                        """
                        UPDATE intents
                           SET status = 'dropped',
                               dropped_reason = :dropped_reason,
                               dropped_at = :dropped_at,
                               updated_at = :updated_at
                         WHERE intent_id = :intent_id
                           AND status IN ('queued','running','blocked')
                        """
                    ),
                    {
                        "dropped_reason": f"superseded_same_action_key:{str(key)}"[:255],
                        "dropped_at": int(drop_ts),
                        "updated_at": int(drop_ts),
                        "intent_id": str(duplicate_row.get("intent_id") or ""),
                    },
                )

        # --- active intent の同一 action key をDB制約で一意化する ---
        conn.execute(text("DROP INDEX IF EXISTS uq_intents_active_canonical_action_key"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_intents_active_canonical_action_key "
                "ON intents(canonical_action_key) "
                "WHERE status IN ('queued','running','blocked') "
                "AND length(trim(COALESCE(canonical_action_key,''))) > 0"
            )
        )

        # --- スキーマバージョンを更新 ---
        conn.execute(text("PRAGMA user_version=17"))
        conn.commit()

    logger.info("memory DB migrated: user_version 16 -> 17")


def migrate_memory_db_if_needed(*, engine, target_user_version: int, logger: logging.Logger) -> None:
    """記憶DBに必要なマイグレーションを適用する。"""

    # --- 既知版を順送りで移行する ---
    while True:
        with engine.connect() as conn:
            current = _get_sqlite_user_version(conn)

        if current == 10 and int(target_user_version) >= 11:
            _migrate_memory_db_v10_to_v11(engine, logger)
            continue
        if current == 11 and int(target_user_version) >= 12:
            _migrate_memory_db_v11_to_v12(engine, logger)
            continue
        if current == 12 and int(target_user_version) >= 13:
            _migrate_memory_db_v12_to_v13(engine, logger)
            continue
        if current == 13 and int(target_user_version) >= 14:
            _migrate_memory_db_v13_to_v14(engine, logger)
            continue
        if current == 14 and int(target_user_version) >= 15:
            _migrate_memory_db_v14_to_v15(engine, logger)
            continue
        if current == 15 and int(target_user_version) >= 16:
            _migrate_memory_db_v15_to_v16(engine, logger)
            continue
        if current == 16 and int(target_user_version) >= 17:
            _migrate_memory_db_v16_to_v17(engine, logger)
            continue
        break
