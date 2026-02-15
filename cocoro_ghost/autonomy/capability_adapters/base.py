"""
Capability adapter の共通契約。

役割:
- Execute 層から capability 実装を呼ぶためのI/Fを固定する。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AdapterExecutionContext:
    """adapter 実行コンテキスト。"""

    goal_id: str | None
    ticket_id: str
    capability_id: str
    operation: str
    trigger_type: str
    issued_at: int
    embedding_preset_id: str
    embedding_dimension: int


@dataclass(frozen=True)
class AdapterExecutionOutput:
    """adapter 実行結果。"""

    result_payload: dict[str, Any]
    effects: list[dict[str, Any]]
    meta_json: dict[str, Any] = field(default_factory=dict)


class CapabilityAdapter(ABC):
    """capability adapter の抽象基底。"""

    @property
    @abstractmethod
    def capability_id(self) -> str:
        """担当する capability_id を返す。"""

    @abstractmethod
    def execute(
        self,
        *,
        context: AdapterExecutionContext,
        input_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> AdapterExecutionOutput:
        """operation を1回だけ実行する。"""

