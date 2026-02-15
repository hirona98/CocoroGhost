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


def _load_existing_enabled_state(*, registry: CapabilityRegistry) -> tuple[dict[str, bool], dict[tuple[str, str], bool]]:
    """既存 capability/operation の enabled 状態を読み出す。"""

    # --- 既存 descriptor を取得 ---
    descriptors = list(registry.list_descriptors())

    # --- capability/operation の enabled マップを作成 ---
    capability_enabled: dict[str, bool] = {}
    operation_enabled: dict[tuple[str, str], bool] = {}
    for desc in list(descriptors or []):
        cap_id = str(desc.capability_id or "").strip()
        if not cap_id:
            continue
        capability_enabled[cap_id] = bool(desc.enabled)
        for op in list(desc.operations or []):
            op_name = str(op.operation or "").strip()
            if not op_name:
                continue
            operation_enabled[(cap_id, op_name)] = bool(op.enabled)
    return capability_enabled, operation_enabled


def _resolve_capability_enabled(
    *,
    capability_id: str,
    default_enabled: bool,
    capability_enabled_by_id: dict[str, bool],
) -> bool:
    """capability の有効/無効を既存状態優先で決定する。"""

    # --- 既存状態を優先 ---
    cap_id = str(capability_id or "").strip()
    if cap_id in capability_enabled_by_id:
        return bool(capability_enabled_by_id[cap_id])
    return bool(default_enabled)


def _resolve_operation_enabled(
    *,
    capability_id: str,
    operation: str,
    default_enabled: bool,
    operation_enabled_by_key: dict[tuple[str, str], bool],
) -> bool:
    """operation の有効/無効を既存状態優先で決定する。"""

    # --- 既存状態を優先 ---
    key = (str(capability_id or "").strip(), str(operation or "").strip())
    if key in operation_enabled_by_key:
        return bool(operation_enabled_by_key[key])
    return bool(default_enabled)


def register_default_capabilities(*, registry: CapabilityRegistry) -> None:
    """標準 capability を registry へ登録する。"""

    # --- 既存 enabled 状態を事前取得 ---
    capability_enabled_by_id, operation_enabled_by_key = _load_existing_enabled_state(registry=registry)

    # --- `speak` descriptor を登録 ---
    registry.register_descriptor(
        descriptor=CapabilityDescriptor(
            capability_id="speak",
            display_name="Speak",
            enabled=_resolve_capability_enabled(
                capability_id="speak",
                default_enabled=True,
                capability_enabled_by_id=capability_enabled_by_id,
            ),
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
                    enabled=_resolve_operation_enabled(
                        capability_id="speak",
                        operation="emit",
                        default_enabled=True,
                        operation_enabled_by_key=operation_enabled_by_key,
                    ),
                )
            ],
        )
    )

    # --- `web_access` descriptor を登録 ---
    registry.register_descriptor(
        descriptor=CapabilityDescriptor(
            capability_id="web_access",
            display_name="Web Access",
            enabled=_resolve_capability_enabled(
                capability_id="web_access",
                default_enabled=True,
                capability_enabled_by_id=capability_enabled_by_id,
            ),
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
                    enabled=_resolve_operation_enabled(
                        capability_id="web_access",
                        operation="search",
                        default_enabled=True,
                        operation_enabled_by_key=operation_enabled_by_key,
                    ),
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
                    enabled=_resolve_operation_enabled(
                        capability_id="web_access",
                        operation="open_url",
                        default_enabled=True,
                        operation_enabled_by_key=operation_enabled_by_key,
                    ),
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
                    enabled=_resolve_operation_enabled(
                        capability_id="web_access",
                        operation="extract_structured",
                        default_enabled=True,
                        operation_enabled_by_key=operation_enabled_by_key,
                    ),
                ),
            ],
        )
    )

    # --- adapter を登録 ---
    registry.register_adapter(adapter=SpeakCapabilityAdapter())
    registry.register_adapter(adapter=WebAccessCapabilityAdapter())
