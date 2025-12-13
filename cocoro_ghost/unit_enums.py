"""Unitベース記憶のEnum定義。"""

from __future__ import annotations

from enum import IntEnum


class UnitKind(IntEnum):
    EPISODE = 1
    FACT = 2
    SUMMARY = 3
    PERSONA = 4
    CONTRACT = 5
    CAPSULE = 6
    LOOP = 7


class UnitState(IntEnum):
    RAW = 0
    VALIDATED = 1
    CONSOLIDATED = 2
    ARCHIVED = 3


class Sensitivity(IntEnum):
    NORMAL = 0
    PRIVATE = 1
    SECRET = 2


class EntityType(IntEnum):
    PERSON = 1
    PLACE = 2
    PROJECT = 3
    WORK = 4
    TOPIC = 5
    ORG = 6


class EntityRole(IntEnum):
    MENTIONED = 1


class RelationType(IntEnum):
    OTHER = 0
    FRIEND = 1
    FAMILY = 2
    COLLEAGUE = 3
    PARTNER = 4
    LIKES = 5
    DISLIKES = 6
    RELATED = 7


class SummaryScopeType(IntEnum):
    DAILY = 1
    WEEKLY = 2
    PERSON = 3
    TOPIC = 4
    RELATIONSHIP = 5


class LoopStatus(IntEnum):
    OPEN = 0
    CLOSED = 1


class JobStatus(IntEnum):
    QUEUED = 0
    RUNNING = 1
    DONE = 2
    FAILED = 3
