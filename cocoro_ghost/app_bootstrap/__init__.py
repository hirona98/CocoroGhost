"""
アプリ起動配線パッケージ。

目的:
    - 起動時の配線を `main.py` から分離する。
    - 初期化手順を責務ごとに読みやすく保つ。
"""

from __future__ import annotations

from cocoro_ghost.app_bootstrap.config_bootstrap import bootstrap_runtime_config
from cocoro_ghost.app_bootstrap.dependencies import (
    get_clock_service_dep,
    get_config_store_dep,
    get_llm_client,
    get_memory_db_dep,
    get_memory_manager,
    get_reminders_db_dep,
    get_settings_db_dep,
    reset_memory_manager,
)
from cocoro_ghost.app_bootstrap.lifecycle import register_lifecycle_hooks
from cocoro_ghost.app_bootstrap.routers import register_http_routes

__all__ = [
    "bootstrap_runtime_config",
    "get_clock_service_dep",
    "get_config_store_dep",
    "get_llm_client",
    "get_memory_db_dep",
    "get_memory_manager",
    "get_reminders_db_dep",
    "get_settings_db_dep",
    "register_http_routes",
    "register_lifecycle_hooks",
    "reset_memory_manager",
]
