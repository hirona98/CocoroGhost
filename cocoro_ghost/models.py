"""ORM モデル定義。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cocoro_ghost.db import Base
from cocoro_ghost.defaults import DEFAULT_EXCLUDE_KEYWORDS_JSON


# --- 設定DB用モデル ---


_UUID_STR_LEN = 36


def _uuid_str() -> str:
    return str(uuid4())


class GlobalSettings(Base):
    """共通設定。"""

    __tablename__ = "global_settings"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exclude_keywords: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_EXCLUDE_KEYWORDS_JSON)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active_llm_preset_id: Mapped[Optional[str]] = mapped_column(String(_UUID_STR_LEN), ForeignKey("llm_presets.id"))
    active_embedding_preset_id: Mapped[Optional[str]] = mapped_column(
        String(_UUID_STR_LEN), ForeignKey("embedding_presets.id")
    )
    active_persona_preset_id: Mapped[Optional[str]] = mapped_column(
        String(_UUID_STR_LEN), ForeignKey("persona_presets.id")
    )
    active_contract_preset_id: Mapped[Optional[str]] = mapped_column(
        String(_UUID_STR_LEN), ForeignKey("contract_presets.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    active_llm_preset: Mapped[Optional["LlmPreset"]] = relationship("LlmPreset", foreign_keys=[active_llm_preset_id])
    active_embedding_preset: Mapped[Optional["EmbeddingPreset"]] = relationship(
        "EmbeddingPreset", foreign_keys=[active_embedding_preset_id]
    )
    active_persona_preset: Mapped[Optional["PersonaPreset"]] = relationship(
        "PersonaPreset", foreign_keys=[active_persona_preset_id]
    )
    active_contract_preset: Mapped[Optional["ContractPreset"]] = relationship(
        "ContractPreset", foreign_keys=[active_contract_preset_id]
    )


class LlmPreset(Base):
    """LLMプリセット。"""

    __tablename__ = "llm_presets"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # LLM設定
    llm_api_key: Mapped[str] = mapped_column(String, nullable=False)
    llm_model: Mapped[str] = mapped_column(String, nullable=False)
    llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String)
    max_turns_window: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    max_tokens_vision: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)

    # Image LLM設定
    image_model: Mapped[str] = mapped_column(String, nullable=False)
    image_model_api_key: Mapped[Optional[str]] = mapped_column(String)
    image_llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    image_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class PersonaPreset(Base):
    """persona プロンプトプリセット。"""

    __tablename__ = "persona_presets"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    persona_text: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContractPreset(Base):
    """contract プロンプトプリセット。"""

    __tablename__ = "contract_presets"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contract_text: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmbeddingPreset(Base):
    """Embeddingプリセット。"""

    __tablename__ = "embedding_presets"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    # name は表示名（memory_id ではない）
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

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

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
