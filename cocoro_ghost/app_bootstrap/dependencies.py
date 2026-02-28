"""
依存オブジェクトの生成。

目的:
    - FastAPI の Depends で使う生成処理を起動配線側に寄せる。
    - シングルトン生成と DI 入口を同じ責務で管理する。
"""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from cocoro_ghost.clock import ClockService, get_clock_service
from cocoro_ghost.config import ConfigStore, get_config_store
from cocoro_ghost.db import get_memory_session, get_settings_db
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.memory import MemoryManager
from cocoro_ghost.reminders.db import get_reminders_db


_memory_manager: MemoryManager | None = None


def get_llm_client() -> LlmClient:
    """
    現在の ConfigStore から LlmClient を生成する。
    """

    # --- 現在の統合設定を読み、クライアントへ転写する ---
    config_store = get_config_store()
    cfg = config_store.config
    return LlmClient(
        model=cfg.llm_model,
        embedding_model=cfg.embedding_model,
        embedding_api_key=cfg.embedding_api_key,
        image_model=cfg.image_model,
        api_key=cfg.llm_api_key,
        llm_base_url=cfg.llm_base_url,
        embedding_base_url=cfg.embedding_base_url,
        image_llm_base_url=cfg.image_llm_base_url,
        image_model_api_key=cfg.image_model_api_key,
        reasoning_effort=cfg.reasoning_effort,
        reply_web_search_enabled=cfg.reply_web_search_enabled,
        max_tokens=cfg.max_tokens,
        max_tokens_vision=cfg.max_tokens_vision,
        image_timeout_seconds=cfg.image_timeout_seconds,
        timeout_seconds=cfg.llm_timeout_seconds,
        stream_timeout_seconds=cfg.llm_stream_timeout_seconds,
    )


def get_memory_manager() -> MemoryManager:
    """
    MemoryManager のシングルトンを返す。
    """

    global _memory_manager

    # --- 初回だけ生成し、以降は同じインスタンスを返す ---
    if _memory_manager is None:
        _memory_manager = MemoryManager(
            llm_client=get_llm_client(),
            config_store=get_config_store(),
        )
    return _memory_manager


def reset_memory_manager() -> None:
    """
    MemoryManager シングルトンを破棄する。
    """

    global _memory_manager
    _memory_manager = None


def get_config_store_dep() -> ConfigStore:
    """
    ConfigStore を Depends 用に返す。
    """

    # --- FastAPI の Depends からそのまま参照できるよう薄い入口にする ---
    return get_config_store()


def get_clock_service_dep() -> ClockService:
    """
    ClockService を Depends 用に返す。
    """

    # --- system/domain 時刻は共有サービスを返す ---
    return get_clock_service()


def get_settings_db_dep() -> Iterator[Session]:
    """
    settings DB セッションを Depends 用に返す。
    """

    # --- 設定 DB は既存の generator をそのまま流す ---
    yield from get_settings_db()


def get_memory_db_dep() -> Iterator[Session]:
    """
    memory DB セッションを Depends 用に返す。
    """

    # --- 現在の embedding 設定に対応する memory DB を開く ---
    config_store = get_config_store()
    session = get_memory_session(
        config_store.embedding_preset_id,
        config_store.embedding_dimension,
    )
    try:
        yield session
    finally:
        session.close()


def get_reminders_db_dep() -> Iterator[Session]:
    """
    reminders DB セッションを Depends 用に返す。
    """

    # --- reminders DB は専用 generator をそのまま流す ---
    yield from get_reminders_db()
