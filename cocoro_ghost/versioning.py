"""unit_versions ユーティリティ。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from cocoro_ghost.unit_models import UnitVersion


def canonical_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_unit_version(
    session: Session,
    *,
    unit_id: int,
    payload_obj: Any,
    patch_reason: str,
    now_ts: int,
) -> None:
    payload_hash = sha256_text(canonical_json_dumps(payload_obj))
    pending_versions: list[UnitVersion] = [
        uv for uv in session.new if isinstance(uv, UnitVersion) and int(uv.unit_id) == int(unit_id)
    ]
    for uv in pending_versions:
        if (uv.payload_hash or "") == payload_hash:
            return

    last = (
        session.query(UnitVersion)
        .filter(UnitVersion.unit_id == unit_id)
        .order_by(UnitVersion.version.desc())
        .first()
    )
    if last is not None and (last.payload_hash or "") == payload_hash:
        return

    last_version = int(last.version) if last is not None else 0
    pending_max = max((int(uv.version) for uv in pending_versions), default=0)
    parent_version = max(last_version, pending_max)
    next_version = parent_version + 1
    session.add(
        UnitVersion(
            unit_id=unit_id,
            version=next_version,
            parent_version=parent_version if parent_version > 0 else None,
            patch_reason=patch_reason,
            payload_hash=payload_hash,
            created_at=now_ts,
        )
    )
