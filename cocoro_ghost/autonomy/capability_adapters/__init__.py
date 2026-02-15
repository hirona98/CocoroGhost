"""
Capability adapter 実装群。
"""

from cocoro_ghost.autonomy.capability_adapters.base import (
    AdapterExecutionContext,
    AdapterExecutionOutput,
    CapabilityAdapter,
)
from cocoro_ghost.autonomy.capability_adapters.speak import SpeakCapabilityAdapter


__all__ = [
    "AdapterExecutionContext",
    "AdapterExecutionOutput",
    "CapabilityAdapter",
    "SpeakCapabilityAdapter",
]

