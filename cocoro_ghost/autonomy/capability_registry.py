"""
Capability Registry（永続化 + adapter解決）。

役割:
- capability/operation 契約を保存・解決する。
- input/result/effect の schema 検証を行う。
- capability adapter を解決して Execute 層へ渡す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from cocoro_ghost import common_utils
from cocoro_ghost.autonomy.capability_adapters.base import CapabilityAdapter
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import WmCapability, WmCapabilityOperation


def _now_utc_ts() -> int:
    """現在UTC時刻をUNIX秒で返す。"""

    return int(time.time())


@dataclass(frozen=True)
class CapabilityOperationDescriptor:
    """operation 単位の契約。"""

    operation: str
    input_schema_json: dict[str, Any] = field(default_factory=dict)
    result_schema_json: dict[str, Any] = field(default_factory=dict)
    effect_schema_json: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30
    enabled: bool = True


@dataclass(frozen=True)
class CapabilityDescriptor:
    """capability 単位の契約。"""

    capability_id: str
    display_name: str
    enabled: bool
    version: str
    metadata_json: dict[str, Any] = field(default_factory=dict)
    operations: list[CapabilityOperationDescriptor] = field(default_factory=list)


def _assert_schema_shape(*, schema: dict[str, Any], capability_id: str, operation: str, schema_name: str) -> None:
    """schema の最小形状を検証する。"""

    # --- schema は object 固定 ---
    schema_type = str(schema.get("type") or "").strip()
    if schema_type != "object":
        raise RuntimeError(
            f"{schema_name}.type must be object capability_id={capability_id} operation={operation}"
        )

    # --- additionalProperties=false を必須にする ---
    additional = schema.get("additionalProperties")
    if additional is not False:
        raise RuntimeError(
            f"{schema_name}.additionalProperties must be false capability_id={capability_id} operation={operation}"
        )

    # --- properties は dict のみ許可 ---
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise RuntimeError(
            f"{schema_name}.properties must be object capability_id={capability_id} operation={operation}"
        )

    # --- required は list[str] のみ許可 ---
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise RuntimeError(
            f"{schema_name}.required must be list capability_id={capability_id} operation={operation}"
        )
    for key in list(required):
        if not str(key or "").strip():
            raise RuntimeError(
                f"{schema_name}.required contains empty key capability_id={capability_id} operation={operation}"
            )


def _check_simple_json_type(*, value: Any, json_type: str) -> bool:
    """簡易JSON型チェックを行う。"""

    # --- JSON Schema の主要型のみを扱う ---
    t = str(json_type or "").strip()
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "null":
        return value is None
    return True


def _validate_payload_against_schema(
    *,
    capability_id: str,
    operation: str,
    schema_name: str,
    schema: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """payload を最小ルールで検証する。"""

    # --- schema 形状を検証 ---
    _assert_schema_shape(
        schema=schema,
        capability_id=capability_id,
        operation=operation,
        schema_name=schema_name,
    )

    # --- payload は object 必須 ---
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{schema_name} payload must be object capability_id={capability_id} operation={operation}"
        )

    # --- required キーを検証 ---
    required = list(schema.get("required", []))
    for key in required:
        key_s = str(key)
        if key_s not in payload:
            raise RuntimeError(
                f"{schema_name} missing required key='{key_s}' capability_id={capability_id} operation={operation}"
            )

    # --- additionalProperties=false のため未知キーを拒否 ---
    properties = schema.get("properties", {})
    allowed_keys = {str(k) for k in properties.keys()}
    unknown_keys = sorted({str(k) for k in payload.keys()} - allowed_keys)
    if unknown_keys:
        raise RuntimeError(
            f"{schema_name} unknown keys={unknown_keys} capability_id={capability_id} operation={operation}"
        )

    # --- properties.type の簡易検証 ---
    for key, value in payload.items():
        prop = properties.get(str(key), {})
        if not isinstance(prop, dict):
            raise RuntimeError(
                f"{schema_name}.properties['{key}'] must be object capability_id={capability_id} operation={operation}"
            )
        expected_type = str(prop.get("type") or "").strip()
        if not expected_type:
            continue
        if not _check_simple_json_type(value=value, json_type=expected_type):
            raise RuntimeError(
                f"{schema_name} type mismatch key='{key}' expected={expected_type} "
                f"capability_id={capability_id} operation={operation}"
            )


class CapabilityRegistry:
    """CapabilityDescriptor の永続化と解決を担う。"""

    def __init__(self, *, embedding_preset_id: str, embedding_dimension: int) -> None:
        # --- DB接続パラメータを保持 ---
        self._embedding_preset_id = str(embedding_preset_id)
        self._embedding_dimension = int(embedding_dimension)

        # --- adapter 登録を保持 ---
        self._adapters: dict[str, CapabilityAdapter] = {}

    def register_adapter(self, *, adapter: CapabilityAdapter) -> None:
        """capability adapter を登録する。"""

        # --- capability_id を正規化して登録 ---
        capability_id = str(adapter.capability_id or "").strip()
        if not capability_id:
            raise RuntimeError("adapter.capability_id is required")
        self._adapters[capability_id] = adapter

    def resolve_adapter(self, *, capability_id: str) -> CapabilityAdapter:
        """capability_id から adapter を解決する。"""

        # --- 登録済み adapter を返す ---
        capability_id_s = str(capability_id or "").strip()
        if not capability_id_s:
            raise RuntimeError("capability_id is required")
        adapter = self._adapters.get(capability_id_s)
        if adapter is None:
            raise RuntimeError(f"adapter not found capability_id={capability_id_s}")
        return adapter

    def register_descriptor(self, *, descriptor: CapabilityDescriptor) -> None:
        """descriptor を upsert する。"""

        # --- 入力正規化 ---
        capability_id = str(descriptor.capability_id or "").strip()
        if not capability_id:
            raise RuntimeError("capability_id is required")
        if not list(descriptor.operations or []):
            raise RuntimeError(f"operations must not be empty capability_id={capability_id}")

        # --- capability を upsert ---
        now_ts = _now_utc_ts()
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            cap = db.query(WmCapability).filter(WmCapability.capability_id == capability_id).one_or_none()
            if cap is None:
                cap = WmCapability(
                    capability_id=capability_id,
                    display_name=str(descriptor.display_name),
                    enabled=(1 if bool(descriptor.enabled) else 0),
                    version=str(descriptor.version or "1"),
                    metadata_json=common_utils.json_dumps(dict(descriptor.metadata_json or {})),
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
                db.add(cap)
            else:
                cap.display_name = str(descriptor.display_name)
                cap.enabled = 1 if bool(descriptor.enabled) else 0
                cap.version = str(descriptor.version or "1")
                cap.metadata_json = common_utils.json_dumps(dict(descriptor.metadata_json or {}))
                cap.updated_at = int(now_ts)
                db.add(cap)

            # --- operation を upsert ---
            for op in list(descriptor.operations or []):
                operation = str(op.operation or "").strip()
                if not operation:
                    raise RuntimeError(f"operation is required capability_id={capability_id}")

                # --- schema の最小形状を事前検証 ---
                _assert_schema_shape(
                    schema=dict(op.input_schema_json or {}),
                    capability_id=capability_id,
                    operation=operation,
                    schema_name="input_schema",
                )
                _assert_schema_shape(
                    schema=dict(op.result_schema_json or {}),
                    capability_id=capability_id,
                    operation=operation,
                    schema_name="result_schema",
                )
                _assert_schema_shape(
                    schema=dict(op.effect_schema_json or {}),
                    capability_id=capability_id,
                    operation=operation,
                    schema_name="effect_schema",
                )

                op_row = (
                    db.query(WmCapabilityOperation)
                    .filter(WmCapabilityOperation.capability_id == capability_id)
                    .filter(WmCapabilityOperation.operation == operation)
                    .one_or_none()
                )
                if op_row is None:
                    op_row = WmCapabilityOperation(
                        capability_id=capability_id,
                        operation=operation,
                        input_schema_json=common_utils.json_dumps(dict(op.input_schema_json or {})),
                        result_schema_json=common_utils.json_dumps(dict(op.result_schema_json or {})),
                        effect_schema_json=common_utils.json_dumps(dict(op.effect_schema_json or {})),
                        timeout_seconds=max(1, int(op.timeout_seconds)),
                        enabled=(1 if bool(op.enabled) else 0),
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                    db.add(op_row)
                else:
                    op_row.input_schema_json = common_utils.json_dumps(dict(op.input_schema_json or {}))
                    op_row.result_schema_json = common_utils.json_dumps(dict(op.result_schema_json or {}))
                    op_row.effect_schema_json = common_utils.json_dumps(dict(op.effect_schema_json or {}))
                    op_row.timeout_seconds = max(1, int(op.timeout_seconds))
                    op_row.enabled = 1 if bool(op.enabled) else 0
                    op_row.updated_at = int(now_ts)
                    db.add(op_row)

    def list_descriptors(self) -> list[CapabilityDescriptor]:
        """登録済み descriptor 一覧を返す。"""

        # --- capability と operation を plain dict へ変換 ---
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            cap_rows = db.query(WmCapability).order_by(WmCapability.capability_id.asc()).all()
            op_rows = db.query(WmCapabilityOperation).order_by(
                WmCapabilityOperation.capability_id.asc(),
                WmCapabilityOperation.operation.asc(),
            ).all()

        caps_plain = [
            {
                "capability_id": str(row.capability_id),
                "display_name": str(row.display_name),
                "enabled": bool(int(row.enabled or 0) == 1),
                "version": str(row.version or "1"),
                "metadata_json": common_utils.json_loads_maybe(str(row.metadata_json or "{}")),
            }
            for row in list(cap_rows or [])
        ]
        ops_plain = [
            {
                "capability_id": str(row.capability_id),
                "operation": str(row.operation),
                "input_schema_json": common_utils.json_loads_maybe(str(row.input_schema_json or "{}")),
                "result_schema_json": common_utils.json_loads_maybe(str(row.result_schema_json or "{}")),
                "effect_schema_json": common_utils.json_loads_maybe(str(row.effect_schema_json or "{}")),
                "timeout_seconds": int(row.timeout_seconds or 30),
                "enabled": bool(int(row.enabled or 0) == 1),
            }
            for row in list(op_rows or [])
        ]

        # --- capability 単位で operation を束ねる ---
        ops_by_cap: dict[str, list[CapabilityOperationDescriptor]] = {}
        for row in list(ops_plain or []):
            cap_id = str(row["capability_id"])
            ops_by_cap.setdefault(cap_id, []).append(
                CapabilityOperationDescriptor(
                    operation=str(row["operation"]),
                    input_schema_json=dict(row["input_schema_json"] or {}),
                    result_schema_json=dict(row["result_schema_json"] or {}),
                    effect_schema_json=dict(row["effect_schema_json"] or {}),
                    timeout_seconds=max(1, int(row["timeout_seconds"])),
                    enabled=bool(row["enabled"]),
                )
            )

        # --- descriptor 配列へ変換 ---
        out: list[CapabilityDescriptor] = []
        for row in list(caps_plain or []):
            cap_id = str(row["capability_id"])
            out.append(
                CapabilityDescriptor(
                    capability_id=cap_id,
                    display_name=str(row["display_name"]),
                    enabled=bool(row["enabled"]),
                    version=str(row["version"]),
                    metadata_json=dict(row["metadata_json"] or {}),
                    operations=list(ops_by_cap.get(cap_id, [])),
                )
            )
        return out

    def set_capability_enabled(self, *, capability_id: str, enabled: bool) -> None:
        """capability の有効/無効を更新する。"""

        # --- 対象 capability を更新 ---
        cap_id = str(capability_id or "").strip()
        if not cap_id:
            raise RuntimeError("capability_id is required")
        now_ts = _now_utc_ts()
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            cap = db.query(WmCapability).filter(WmCapability.capability_id == cap_id).one_or_none()
            if cap is None:
                raise RuntimeError(f"capability not found: {cap_id}")
            cap.enabled = 1 if bool(enabled) else 0
            cap.updated_at = int(now_ts)
            db.add(cap)

    def set_operation_enabled(self, *, capability_id: str, operation: str, enabled: bool) -> None:
        """operation の有効/無効を更新する。"""

        # --- 対象 operation を更新 ---
        cap_id = str(capability_id or "").strip()
        op_name = str(operation or "").strip()
        if not cap_id or not op_name:
            raise RuntimeError("capability_id and operation are required")
        now_ts = _now_utc_ts()
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = (
                db.query(WmCapabilityOperation)
                .filter(WmCapabilityOperation.capability_id == cap_id)
                .filter(WmCapabilityOperation.operation == op_name)
                .one_or_none()
            )
            if row is None:
                raise RuntimeError(f"operation not found capability_id={cap_id} operation={op_name}")
            row.enabled = 1 if bool(enabled) else 0
            row.updated_at = int(now_ts)
            db.add(row)

    def resolve_operation(self, *, capability_id: str, operation: str) -> CapabilityOperationDescriptor:
        """capability + operation を解決して descriptor を返す。"""

        # --- 入力正規化 ---
        cap_id = str(capability_id or "").strip()
        op_name = str(operation or "").strip()
        if not cap_id or not op_name:
            raise RuntimeError("capability_id and operation are required")

        # --- capability / operation の有効性を検証 ---
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            cap = db.query(WmCapability).filter(WmCapability.capability_id == cap_id).one_or_none()
            if cap is None:
                raise RuntimeError(f"capability not found: {cap_id}")
            if int(cap.enabled or 0) != 1:
                raise RuntimeError(f"capability disabled: {cap_id}")

            row = (
                db.query(WmCapabilityOperation)
                .filter(WmCapabilityOperation.capability_id == cap_id)
                .filter(WmCapabilityOperation.operation == op_name)
                .one_or_none()
            )
            if row is None:
                raise RuntimeError(f"operation not found capability_id={cap_id} operation={op_name}")
            if int(row.enabled or 0) != 1:
                raise RuntimeError(f"operation disabled capability_id={cap_id} operation={op_name}")

            # --- descriptor へ変換 ---
            descriptor = CapabilityOperationDescriptor(
                operation=str(row.operation),
                input_schema_json=dict(common_utils.json_loads_maybe(str(row.input_schema_json or "{}")) or {}),
                result_schema_json=dict(common_utils.json_loads_maybe(str(row.result_schema_json or "{}")) or {}),
                effect_schema_json=dict(common_utils.json_loads_maybe(str(row.effect_schema_json or "{}")) or {}),
                timeout_seconds=max(1, int(row.timeout_seconds or 30)),
                enabled=True,
            )

        # --- schema 形状を検証して返す ---
        _assert_schema_shape(
            schema=dict(descriptor.input_schema_json or {}),
            capability_id=cap_id,
            operation=op_name,
            schema_name="input_schema",
        )
        _assert_schema_shape(
            schema=dict(descriptor.result_schema_json or {}),
            capability_id=cap_id,
            operation=op_name,
            schema_name="result_schema",
        )
        _assert_schema_shape(
            schema=dict(descriptor.effect_schema_json or {}),
            capability_id=cap_id,
            operation=op_name,
            schema_name="effect_schema",
        )
        return descriptor

    def get_operation_descriptor(self, *, capability_id: str, operation: str) -> CapabilityOperationDescriptor:
        """operation descriptor を返す。"""

        return self.resolve_operation(capability_id=capability_id, operation=operation)

    def list_enabled_capabilities(self) -> list[CapabilityDescriptor]:
        """有効な capability 一覧を返す。"""

        # --- descriptor から enabled なものだけを返す ---
        out: list[CapabilityDescriptor] = []
        for desc in self.list_descriptors():
            if not bool(desc.enabled):
                continue
            enabled_ops = [op for op in list(desc.operations or []) if bool(op.enabled)]
            if not enabled_ops:
                continue
            out.append(
                CapabilityDescriptor(
                    capability_id=str(desc.capability_id),
                    display_name=str(desc.display_name),
                    enabled=True,
                    version=str(desc.version),
                    metadata_json=dict(desc.metadata_json or {}),
                    operations=list(enabled_ops),
                )
            )
        return out

    def validate_input_payload(self, *, capability_id: str, operation: str, payload: dict[str, Any]) -> None:
        """input payload を schema 検証する。"""

        # --- descriptor 取得 ---
        op = self.resolve_operation(capability_id=capability_id, operation=operation)

        # --- payload 検証 ---
        _validate_payload_against_schema(
            capability_id=capability_id,
            operation=operation,
            schema_name="input_schema",
            schema=dict(op.input_schema_json or {}),
            payload=dict(payload or {}),
        )

    def validate_result_payload(self, *, capability_id: str, operation: str, payload: dict[str, Any]) -> None:
        """result payload を schema 検証する。"""

        # --- descriptor 取得 ---
        op = self.resolve_operation(capability_id=capability_id, operation=operation)

        # --- payload 検証 ---
        _validate_payload_against_schema(
            capability_id=capability_id,
            operation=operation,
            schema_name="result_schema",
            schema=dict(op.result_schema_json or {}),
            payload=dict(payload or {}),
        )

    def validate_effects(self, *, capability_id: str, operation: str, effects: list[dict[str, Any]]) -> None:
        """effects 配列を schema 検証する。"""

        # --- descriptor 取得 ---
        op = self.resolve_operation(capability_id=capability_id, operation=operation)
        schema = dict(op.effect_schema_json or {})

        # --- effects は list 必須 ---
        if not isinstance(effects, list):
            raise RuntimeError(
                f"effects must be list capability_id={capability_id} operation={operation}"
            )

        # --- 各要素を object として検証 ---
        for effect in list(effects or []):
            if not isinstance(effect, dict):
                raise RuntimeError(
                    f"effect item must be object capability_id={capability_id} operation={operation}"
                )
            _validate_payload_against_schema(
                capability_id=capability_id,
                operation=operation,
                schema_name="effect_schema",
                schema=schema,
                payload=dict(effect),
            )

