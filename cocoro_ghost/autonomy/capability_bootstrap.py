"""
自律ループの標準 capability 登録。

役割:
- Phase 6/7 の標準 capability descriptor と adapter を登録する。
"""

from __future__ import annotations

from cocoro_ghost.autonomy.capability_adapters.speak import SpeakCapabilityAdapter
from cocoro_ghost.autonomy.capability_adapters.web_access import WebAccessCapabilityAdapter
from cocoro_ghost.autonomy.capability_registry import (
    CapabilityDescriptor,
    CapabilityOperationDescriptor,
    CapabilityRegistry,
)


def register_default_capabilities(*, registry: CapabilityRegistry) -> None:
    """標準 capability を registry へ登録する。"""

    # --- `speak` descriptor を登録 ---
    registry.register_descriptor(
        descriptor=CapabilityDescriptor(
            capability_id="speak",
            display_name="Speak",
            enabled=True,
            version="1",
            metadata_json={"owner": "phase6"},
            operations=[
                CapabilityOperationDescriptor(
                    operation="emit",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["message"],
                        "properties": {
                            "message": {"type": "string"},
                            "target_client_id": {"type": "string"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["event_id", "source", "message"],
                        "properties": {
                            "event_id": {"type": "integer"},
                            "source": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "event_id"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "event_id": {"type": "integer"},
                            "source": {"type": "string"},
                            "stream_type": {"type": "string"},
                        },
                    },
                    timeout_seconds=10,
                    enabled=True,
                )
            ],
        )
    )

    # --- `web_access` descriptor を登録 ---
    registry.register_descriptor(
        descriptor=CapabilityDescriptor(
            capability_id="web_access",
            display_name="Web Access",
            enabled=True,
            version="1",
            metadata_json={"owner": "phase7"},
            operations=[
                CapabilityOperationDescriptor(
                    operation="search",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query", "top_k", "recency_days", "domains", "locale"],
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer"},
                            "recency_days": {},
                            "domains": {"type": "array"},
                            "locale": {"type": "string"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["items"],
                        "properties": {
                            "items": {"type": "array"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "query", "urls"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "query": {"type": "string"},
                            "urls": {"type": "array"},
                        },
                    },
                    timeout_seconds=20,
                    enabled=True,
                ),
                CapabilityOperationDescriptor(
                    operation="open_url",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url", "max_chars"],
                        "properties": {
                            "url": {"type": "string"},
                            "max_chars": {"type": "integer"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url", "title", "text", "fetched_at"],
                        "properties": {
                            "url": {"type": "string"},
                            "title": {"type": "string"},
                            "text": {"type": "string"},
                            "fetched_at": {"type": "string"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "url", "text_digest"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "url": {"type": "string"},
                            "text_digest": {"type": "string"},
                        },
                    },
                    timeout_seconds=20,
                    enabled=True,
                ),
                CapabilityOperationDescriptor(
                    operation="extract_structured",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_url", "source_text", "target_schema_json"],
                        "properties": {
                            "source_url": {"type": "string"},
                            "source_text": {"type": "string"},
                            "target_schema_json": {"type": "object"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_url", "structured_data_json"],
                        "properties": {
                            "source_url": {"type": "string"},
                            "structured_data_json": {"type": "object"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "source_url", "keys"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "source_url": {"type": "string"},
                            "keys": {"type": "array"},
                        },
                    },
                    timeout_seconds=30,
                    enabled=True,
                ),
            ],
        )
    )

    # --- adapter を登録 ---
    registry.register_adapter(adapter=SpeakCapabilityAdapter())
    registry.register_adapter(adapter=WebAccessCapabilityAdapter())

