"""Unitベース記憶のORMモデル。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from cocoro_ghost.db import UnitBase


class Unit(UnitBase):
    """Unit本体（全payload共通のメタ）。"""
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
    """Episode（対話ログ）の本文payload。"""
    __tablename__ = "payload_episode"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    user_text: Mapped[Optional[str]] = mapped_column(Text)
    reply_text: Mapped[Optional[str]] = mapped_column(Text)
    image_summary: Mapped[Optional[str]] = mapped_column(Text)
    context_note: Mapped[Optional[str]] = mapped_column(Text)
    reflection_json: Mapped[Optional[str]] = mapped_column(Text)


class PayloadFact(UnitBase):
    """Fact（三つ組の知識）のpayload。"""
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
    """Summary（要約）のpayload。"""
    __tablename__ = "payload_summary"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    # 固定Enumではなく自由ラベル（将来のスコープ追加に耐える）
    scope_label: Mapped[str] = mapped_column(Text, nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    range_start: Mapped[Optional[int]] = mapped_column(Integer)
    range_end: Mapped[Optional[int]] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[Optional[str]] = mapped_column(Text)

class PayloadCapsule(UnitBase):
    """Capsule（期限付きメモ/状態）のpayload。"""
    __tablename__ = "payload_capsule"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    expires_at: Mapped[Optional[int]] = mapped_column(Integer)
    capsule_json: Mapped[str] = mapped_column(Text, nullable=False)


class PayloadLoop(UnitBase):
    """Loop（反復/未解決ループ）のpayload。"""
    __tablename__ = "payload_loop"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[Optional[int]] = mapped_column(Integer)
    loop_text: Mapped[str] = mapped_column(Text, nullable=False)


class Entity(UnitBase):
    """エンティティ（人/場所/話題など）のマスタ。"""
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 固定Enumをやめ、自由なラベル + rolesで扱う（パートナーAI用途）。
    type_label: Mapped[Optional[str]] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[Optional[str]] = mapped_column(Text)
    roles_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class EntityAlias(UnitBase):
    """エンティティの別名（同一人物の呼び名など）。"""
    __tablename__ = "entity_aliases"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    alias: Mapped[str] = mapped_column(Text, primary_key=True)


class UnitEntity(UnitBase):
    """UnitとEntityの関連（言及など）を表す中間テーブル。"""
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
    """エンティティ間の関係（グラフエッジ）。"""
    __tablename__ = "edges"

    src_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 固定Enumではなく自由ラベル（"friend"/"likes"/"mentor" など）
    rel_label: Mapped[str] = mapped_column(Text, primary_key=True)
    dst_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    first_seen_at: Mapped[Optional[int]] = mapped_column(Integer)
    last_seen_at: Mapped[Optional[int]] = mapped_column(Integer)
    evidence_unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id"))


class UnitVersion(UnitBase):
    """Unitのバージョン履歴（差分理由/ハッシュなど）。"""
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
    """非同期処理用ジョブ（Workerが実行）。"""
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
