"""依存オブジェクトの生成。"""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from cocoro_ghost.config import ConfigStore, get_config_store
from cocoro_ghost.db import get_memory_session, get_settings_db, init_memory_db
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.memory import MemoryManager


_memory_manager: MemoryManager | None = None


def get_llm_client() -> LlmClient:
    """ConfigStoreからLlmClientを生成。"""
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
        max_tokens=cfg.max_tokens,
        max_tokens_vision=cfg.max_tokens_vision,
        image_timeout_seconds=cfg.image_timeout_seconds,
    )


def get_memory_manager() -> MemoryManager:
    """MemoryManagerのシングルトンを取得。"""
    global _memory_manager
    if _memory_manager is None:
        config_store = get_config_store()
        llm_client = get_llm_client()
        _memory_manager = MemoryManager(llm_client=llm_client, config_store=config_store)
    return _memory_manager


def reset_memory_manager() -> None:
    """MemoryManagerをリセット（設定変更時などに使用）。"""
    global _memory_manager
    _memory_manager = None


def get_config_store_dep() -> ConfigStore:
    """FastAPI依存性注入用。"""
    return get_config_store()


def get_settings_db_dep() -> Iterator[Session]:
    """設定DBセッションのFastAPI依存性注入用。"""
    yield from get_settings_db()


def get_memory_db_dep() -> Iterator[Session]:
    """記憶DBセッションのFastAPI依存性注入用。"""
    config_store = get_config_store()
    session = get_memory_session(config_store.memory_id, config_store.embedding_dimension)
    try:
        yield session
    finally:
        session.close()
