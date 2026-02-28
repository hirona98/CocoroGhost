"""
起動時の設定・DB初期化。

目的:
    - create_app() から初期化の詳細を切り離す。
    - 設定DB -> プリセット -> 記憶DB の順序を1箇所で固定する。
"""

from __future__ import annotations

from cocoro_ghost.config import (
    ConfigStore,
    RuntimeConfig,
    build_runtime_config,
    load_config,
    set_global_config_store,
)
from cocoro_ghost.storage.db import (
    ensure_initial_settings,
    init_memory_db,
    init_settings_db,
    load_active_addon_preset,
    load_active_embedding_preset,
    load_active_llm_preset,
    load_active_persona_preset,
    load_global_settings,
    settings_session_scope,
)
from cocoro_ghost.runtime.logging import setup_logging
from cocoro_ghost.reminders.db import init_reminders_db, reminders_session_scope
from cocoro_ghost.reminders.repo import ensure_initial_reminder_global_settings


def bootstrap_runtime_config() -> RuntimeConfig:
    """
    起動時の初期化を実行し、確定した RuntimeConfig を返す。

    Returns:
        起動完了後にアプリ全体で使う RuntimeConfig。
    """

    # --- 1. TOML 設定を読み込み、ログ設定を先に確定する ---
    toml_config = load_config()
    setup_logging(
        toml_config.log_level,
        log_file_enabled=toml_config.log_file_enabled,
        log_file_path=toml_config.log_file_path,
        log_file_max_bytes=toml_config.log_file_max_bytes,
    )

    # --- 2. settings / reminders DB を初期化する ---
    init_settings_db()
    init_reminders_db()

    # --- 3. 初期設定行を作成する ---
    with settings_session_scope() as session:
        ensure_initial_settings(session, toml_config)
    with reminders_session_scope() as session:
        ensure_initial_reminder_global_settings(session)

    # --- 4. アクティブ設定を読み、RuntimeConfig を構築する ---
    with settings_session_scope() as session:
        global_settings = load_global_settings(session)
        llm_preset = load_active_llm_preset(session)
        embedding_preset = load_active_embedding_preset(session)
        persona_preset = load_active_persona_preset(session)
        addon_preset = load_active_addon_preset(session)
        runtime_config = build_runtime_config(
            toml_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            addon_preset,
        )
        config_store = ConfigStore(
            toml_config=toml_config,
            runtime_config=runtime_config,
        )

    # --- 5. グローバル設定ストア登録後に、記憶 DB を初期化する ---
    set_global_config_store(config_store)
    init_memory_db(
        runtime_config.embedding_preset_id,
        runtime_config.embedding_dimension,
    )
    return runtime_config
