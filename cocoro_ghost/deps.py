"""依存オブジェクトの生成。"""

from __future__ import annotations

from cocoro_ghost.config import ConfigStore, get_config_store
from cocoro_ghost.db import init_db
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.memory import MemoryManager


_memory_manager: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        config_store = get_config_store()
        init_db(config_store.config.db_url)
        llm_client = LlmClient(
            model=config_store.config.llm_model,
            reflection_model=config_store.config.reflection_model,
            embedding_model=config_store.config.embedding_model,
        )
        _memory_manager = MemoryManager(llm_client=llm_client, config_store=config_store)
    return _memory_manager


def get_config_store_dep() -> ConfigStore:
    return get_config_store()
