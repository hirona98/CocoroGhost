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
        # --- global_settings に gmini backend 実行コマンド列を追加 ---
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


def migrate_memory_db_if_needed(*, engine, target_user_version: int, logger: logging.Logger) -> None:
    """記憶DBに必要なマイグレーションを適用する。"""

    # --- 現時点では v10 -> v11 のみサポート ---
    while True:
        with engine.connect() as conn:
            current = _get_sqlite_user_version(conn)

        if current == 10 and int(target_user_version) >= 11:
            _migrate_memory_db_v10_to_v11(engine, logger)
            continue
        break
