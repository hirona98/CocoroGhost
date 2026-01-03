"""
依存オブジェクトの生成

FastAPIの依存性注入（Depends）で使用するファクトリ関数群。
LlmClient、MemoryManager、ConfigStore、DBセッションの取得を提供する。
MemoryManagerはシングルトンとして管理される。
"""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from cocoro_ghost.config import ConfigStore, get_config_store
from cocoro_ghost.db import get_memory_session, get_settings_db, init_memory_db
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.memory import MemoryManager
from cocoro_ghost.reminders_db import get_reminders_db


_memory_manager: MemoryManager | None = None


def get_llm_client() -> LlmClient:
    """
    ConfigStoreからLlmClientを生成する。

    設定ストアから現在の設定を取得し、LLM/埋め込み/画像モデルの
    APIキー・エンドポイント・パラメータを設定したクライアントを返す。
    """
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
    """
    MemoryManagerのシングルトンを取得する。

    初回呼び出し時にインスタンスを生成し、以降は同じインスタンスを返す。
    設定変更時はreset_memory_manager()でリセットが必要。
    """
    global _memory_manager
    if _memory_manager is None:
        config_store = get_config_store()
        llm_client = get_llm_client()
        _memory_manager = MemoryManager(llm_client=llm_client, config_store=config_store)
    return _memory_manager


def reset_memory_manager() -> None:
    """
    MemoryManagerをリセットする。

    設定変更時などに呼び出し、次回get_memory_manager()で新しいインスタンスを生成させる。
    """
    global _memory_manager
    _memory_manager = None


def get_config_store_dep() -> ConfigStore:
    """
    ConfigStoreをFastAPI依存性注入で取得する。

    Depends(get_config_store_dep)として使用し、エンドポイントで設定にアクセスする。
    """
    return get_config_store()


def get_settings_db_dep() -> Iterator[Session]:
    """
    設定DBセッションをFastAPI依存性注入で取得する。

    リクエスト終了時に自動でセッションがクローズされる。
    """
    yield from get_settings_db()


def get_memory_db_dep() -> Iterator[Session]:
    """
    記憶DBセッションをFastAPI依存性注入で取得する。

    現在のembedding_preset_idに対応するDBファイルへのセッションを提供する。
    リクエスト終了時に自動でセッションがクローズされる。
    """
    config_store = get_config_store()
    session = get_memory_session(config_store.embedding_preset_id, config_store.embedding_dimension)
    try:
        yield session
    finally:
        session.close()


def get_reminders_db_dep() -> Iterator[Session]:
    """
    リマインダーDBセッションをFastAPI依存性注入で取得する。

    reminders.db は settings.db と分離しているため、専用の依存性を用意する。
    """
    yield from get_reminders_db()
