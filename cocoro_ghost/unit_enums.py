"""Unitベース記憶のEnum定義。"""

from __future__ import annotations

from enum import IntEnum


class UnitKind(IntEnum):
    """Unitの種類（Episode/Fact/Summary/...）。"""
    EPISODE = 1
    FACT = 2
    SUMMARY = 3
    CAPSULE = 6
    LOOP = 7


class UnitState(IntEnum):
    """Unitのライフサイクル状態。"""
    RAW = 0
    VALIDATED = 1
    CONSOLIDATED = 2
    ARCHIVED = 3


class Sensitivity(IntEnum):
    """秘匿度（高いほど外部に出しにくい）。"""
    NORMAL = 0
    PRIVATE = 1
    SECRET = 2


class EntityRole(IntEnum):
    """UnitとEntityの関係（現状は言及のみ）。"""
    MENTIONED = 1


class LoopStatus(IntEnum):
    """ループ（未解決タスク/気掛かり等）の状態。"""
    OPEN = 0
    CLOSED = 1


class JobStatus(IntEnum):
    """jobsテーブルの実行状態。"""
    QUEUED = 0
    RUNNING = 1
    DONE = 2
    FAILED = 3
