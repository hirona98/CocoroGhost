"""
Capability adapter 実装群。
"""

from cocoro_ghost.autonomy.capability_adapters.base import (
    AdapterExecutionContext,
    AdapterExecutionOutput,
    CapabilityAdapter,
)
from cocoro_ghost.autonomy.capability_adapters.speak import SpeakCapabilityAdapter
from cocoro_ghost.autonomy.capability_adapters.web_access import WebAccessCapabilityAdapter


__all__ = [
    "AdapterExecutionContext",
    "AdapterExecutionOutput",
    "CapabilityAdapter",
    "SpeakCapabilityAdapter",
    "WebAccessCapabilityAdapter",
]
