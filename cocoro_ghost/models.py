"""ORM モデル定義。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cocoro_ghost.db import Base, MemoryBase


# --- 記憶DB用モデル ---


class Episode(MemoryBase):
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String, nullable=False)
    user_text: Mapped[Optional[str]] = mapped_column(Text)
    reply_text: Mapped[Optional[str]] = mapped_column(Text)
    image_summary: Mapped[Optional[str]] = mapped_column(Text)
    activity: Mapped[Optional[str]] = mapped_column(Text)
    context_note: Mapped[Optional[str]] = mapped_column(Text)
    emotion_label: Mapped[Optional[str]] = mapped_column(String)
    emotion_intensity: Mapped[Optional[float]] = mapped_column(Float)
    topic_tags: Mapped[Optional[str]] = mapped_column(Text)
    reflection_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reflection_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    salience_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    episode_comment: Mapped[Optional[str]] = mapped_column(Text)
    # ベクトル検索用の埋め込みの「バックアップ」(JSONをbytes化したもの)。
    # sqlite-vec の仮想テーブル（episode_vectors）とは別物なので、名前を分けて混同を避ける。
    embedding_json_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    raw_desktop_path: Mapped[Optional[str]] = mapped_column(Text)
    raw_camera_path: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    persons: Mapped[List["EpisodePerson"]] = relationship("EpisodePerson", back_populates="episode")


class Person(MemoryBase):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    is_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    aliases: Mapped[Optional[str]] = mapped_column(Text)
    display_name: Mapped[Optional[str]] = mapped_column(Text)
    relation_to_user: Mapped[Optional[str]] = mapped_column(Text)
    relation_confidence: Mapped[Optional[float]] = mapped_column(Float)
    residence: Mapped[Optional[str]] = mapped_column(Text)
    occupation: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    mention_count: Mapped[Optional[int]] = mapped_column(Integer)
    topic_tags: Mapped[Optional[str]] = mapped_column(Text)
    status_note: Mapped[Optional[str]] = mapped_column(Text)
    closeness_score: Mapped[Optional[float]] = mapped_column(Float)
    worry_score: Mapped[Optional[float]] = mapped_column(Float)
    profile_embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    episodes: Mapped[List["EpisodePerson"]] = relationship("EpisodePerson", back_populates="person")


class EpisodePerson(MemoryBase):
    __tablename__ = "episode_persons"

    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), primary_key=True)
    role: Mapped[Optional[str]] = mapped_column(Text)

    episode: Mapped[Episode] = relationship("Episode", back_populates="persons")
    person: Mapped[Person] = relationship("Person", back_populates="episodes")


# --- 設定DB用モデル ---


class GlobalSettings(Base):
    """共通設定。"""

    __tablename__ = "global_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exclude_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active_llm_preset_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("llm_presets.id"))
    active_character_preset_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("character_presets.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    active_llm_preset: Mapped[Optional["LlmPreset"]] = relationship("LlmPreset", foreign_keys=[active_llm_preset_id])
    active_character_preset: Mapped[Optional["CharacterPreset"]] = relationship(
        "CharacterPreset", foreign_keys=[active_character_preset_id]
    )


class LlmPreset(Base):
    """LLMプリセット。"""

    __tablename__ = "llm_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    # LLM設定
    llm_api_key: Mapped[str] = mapped_column(String, nullable=False)
    llm_model: Mapped[str] = mapped_column(String, nullable=False)
    llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String)
    max_turns_window: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_tokens_vision: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)

    # Embedding設定
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding_api_key: Mapped[Optional[str]] = mapped_column(String)
    embedding_base_url: Mapped[Optional[str]] = mapped_column(String)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)

    # Image LLM設定
    image_model: Mapped[str] = mapped_column(String, nullable=False)
    image_model_api_key: Mapped[Optional[str]] = mapped_column(String)
    image_llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    image_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # 記憶検索パラメータ
    similar_episodes_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_inject_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1200)
    similar_limit_by_kind_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CharacterPreset(Base):
    """キャラクタープリセット。"""

    __tablename__ = "character_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    memory_id: Mapped[str] = mapped_column(String, nullable=False, default="default")

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
