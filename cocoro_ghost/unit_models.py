"""Unitベース記憶のORMモデル。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from cocoro_ghost.db import UnitBase


class Unit(UnitBase):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    source: Mapped[Optional[str]] = mapped_column(Text)
    state: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sensitivity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    topic_tags: Mapped[Optional[str]] = mapped_column(Text)
    emotion_label: Mapped[Optional[str]] = mapped_column(Text)
    emotion_intensity: Mapped[Optional[float]] = mapped_column(Float)


class PayloadEpisode(UnitBase):
    __tablename__ = "payload_episode"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    user_text: Mapped[Optional[str]] = mapped_column(Text)
    reply_text: Mapped[Optional[str]] = mapped_column(Text)
    image_summary: Mapped[Optional[str]] = mapped_column(Text)
    context_note: Mapped[Optional[str]] = mapped_column(Text)
    reflection_json: Mapped[Optional[str]] = mapped_column(Text)


class PayloadFact(UnitBase):
    __tablename__ = "payload_fact"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    subject_entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entities.id"))
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_text: Mapped[Optional[str]] = mapped_column(Text)
    object_entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entities.id"))
    valid_from: Mapped[Optional[int]] = mapped_column(Integer)
    valid_to: Mapped[Optional[int]] = mapped_column(Integer)
    evidence_unit_ids_json: Mapped[str] = mapped_column(Text, nullable=False)


class PayloadSummary(UnitBase):
    __tablename__ = "payload_summary"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    scope_type: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    range_start: Mapped[Optional[int]] = mapped_column(Integer)
    range_end: Mapped[Optional[int]] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[Optional[str]] = mapped_column(Text)


class PayloadPersona(UnitBase):
    __tablename__ = "payload_persona"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    persona_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PayloadContract(UnitBase):
    __tablename__ = "payload_contract"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    contract_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PayloadCapsule(UnitBase):
    __tablename__ = "payload_capsule"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    expires_at: Mapped[Optional[int]] = mapped_column(Integer)
    capsule_json: Mapped[str] = mapped_column(Text, nullable=False)


class PayloadLoop(UnitBase):
    __tablename__ = "payload_loop"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[Optional[int]] = mapped_column(Integer)
    loop_text: Mapped[str] = mapped_column(Text, nullable=False)


class Entity(UnitBase):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    etype: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class EntityAlias(UnitBase):
    __tablename__ = "entity_aliases"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    alias: Mapped[str] = mapped_column(Text, primary_key=True)


class UnitEntity(UnitBase):
    __tablename__ = "unit_entities"

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[int] = mapped_column(Integer, primary_key=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class Edge(UnitBase):
    __tablename__ = "edges"

    src_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    rel_type: Mapped[int] = mapped_column(Integer, primary_key=True)
    dst_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    first_seen_at: Mapped[Optional[int]] = mapped_column(Integer)
    last_seen_at: Mapped[Optional[int]] = mapped_column(Integer)
    evidence_unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id"))


class UnitVersion(UnitBase):
    __tablename__ = "unit_versions"

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_version: Mapped[Optional[int]] = mapped_column(Integer)
    patch_reason: Mapped[Optional[str]] = mapped_column(Text)
    payload_hash: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Job(UnitBase):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[int] = mapped_column(Integer, nullable=False)
    tries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
