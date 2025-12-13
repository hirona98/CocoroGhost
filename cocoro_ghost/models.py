"""ORM モデル定義。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cocoro_ghost.db import Base


# --- 設定DB用モデル ---


class GlobalSettings(Base):
    """共通設定。"""

    __tablename__ = "global_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exclude_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active_llm_preset_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("llm_presets.id"))
    active_embedding_preset_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("embedding_presets.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    active_llm_preset: Mapped[Optional["LlmPreset"]] = relationship("LlmPreset", foreign_keys=[active_llm_preset_id])
    active_embedding_preset: Mapped[Optional["EmbeddingPreset"]] = relationship(
        "EmbeddingPreset", foreign_keys=[active_embedding_preset_id]
    )


class LlmPreset(Base):
    """LLMプリセット。"""

    __tablename__ = "llm_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # LLM設定
    llm_api_key: Mapped[str] = mapped_column(String, nullable=False)
    llm_model: Mapped[str] = mapped_column(String, nullable=False)
    llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String)
    max_turns_window: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_tokens_vision: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)

    # Image LLM設定
    image_model: Mapped[str] = mapped_column(String, nullable=False)
    image_model_api_key: Mapped[Optional[str]] = mapped_column(String)
    image_llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    image_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmbeddingPreset(Base):
    """Embeddingプリセット。"""

    __tablename__ = "embedding_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # name は memory_id として扱う（`memory_<name>.db` の <name> 部分）
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    # Embedding設定
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding_api_key: Mapped[Optional[str]] = mapped_column(String)
    embedding_base_url: Mapped[Optional[str]] = mapped_column(String)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)

    # 記憶検索パラメータ
    similar_episodes_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_inject_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1200)
    similar_limit_by_kind_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Reminder(Base):
    """リマインダー設定。"""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
