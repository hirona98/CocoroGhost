"""
自律ループの層間データ契約。

役割:
- 戦略層/戦術層/実行層の受け渡し構造を固定する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrategicGoalContract:
    """戦略層の目標契約。"""

    goal_id: str
    title: str
    intent: str
    priority: float
    horizon_seconds: int
    success_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    status: str = "active"
    updated_at: int = 0


@dataclass(frozen=True)
class ActionTicketContract:
    """戦術層が発行する実行単位の契約。"""

    ticket_id: str
    goal_id: str
    capability_id: str
    operation: str
    input_payload: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    expected_effect: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    issued_at: int = 0
    deadline_at: int | None = None


@dataclass(frozen=True)
class ActionResultContract:
    """実行層の標準結果契約。"""

    ticket_id: str
    status: str
    observations: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    reason_code: str | None = None
    finished_at: int = 0
