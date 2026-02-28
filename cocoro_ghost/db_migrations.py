"""
DB マイグレーション処理（設定DB / 記憶DB）。

役割:
- db.py から呼び出される SQLite user_version ベースの段階的マイグレーションを提供する。
- 起動時に必要な版だけを順送りで更新する。
"""

from __future__ import annotations

import logging

from sqlalchemy import text


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
                    report_candidate_level TEXT NOT NULL DEFAULT 'none',
                    report_candidate_reason TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    CONSTRAINT pk_agenda_threads PRIMARY KEY (thread_id),
                    CONSTRAINT uq_agenda_threads_thread_key UNIQUE (thread_key),
                    CONSTRAINT ck_agenda_threads_status CHECK (
                        status IN ('open','active','blocked','satisfied','stale','closed')
                    ),
                    CONSTRAINT ck_agenda_threads_report_candidate_level CHECK (
                        report_candidate_level IN ('none','mention','notify','chat')
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
                "CREATE INDEX IF NOT EXISTS idx_agenda_threads_report_candidate_level "
                "ON agenda_threads(report_candidate_level)"
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
        break
