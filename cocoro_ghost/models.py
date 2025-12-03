"""ORM モデル定義。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cocoro_ghost.db import Base


class Episode(Base):
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
    episode_embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    raw_desktop_path: Mapped[Optional[str]] = mapped_column(Text)
    raw_camera_path: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    persons: Mapped[List["EpisodePerson"]] = relationship("EpisodePerson", back_populates="episode")


class Person(Base):
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


class EpisodePerson(Base):
    __tablename__ = "episode_persons"

    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), primary_key=True)
    role: Mapped[Optional[str]] = mapped_column(Text)

    episode: Mapped[Episode] = relationship("Episode", back_populates="persons")
    person: Mapped[Person] = relationship("Person", back_populates="episodes")
