"""管理API（Unit閲覧・編集）。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope, sync_unit_vector_metadata
from cocoro_ghost.deps import get_config_store_dep
from cocoro_ghost.topic_tags import canonicalize_topic_tags_json

from cocoro_ghost.schemas import (
    UnitDetailResponse,
    UnitListResponse,
    UnitMeta,
    UnitUpdateRequest,
)
from cocoro_ghost.unit_enums import UnitKind
from cocoro_ghost.unit_models import (
    PayloadCapsule,
    PayloadEpisode,
    PayloadFact,
    PayloadLoop,
    PayloadSummary,
    Unit,
)
from cocoro_ghost.versioning import record_unit_version


router = APIRouter()


def _to_unit_meta(u: Unit) -> UnitMeta:
    return UnitMeta(
        id=int(u.id),
        kind=int(u.kind),
        occurred_at=int(u.occurred_at) if u.occurred_at is not None else None,
        created_at=int(u.created_at),
        updated_at=int(u.updated_at),
        source=u.source,
        state=int(u.state),
        confidence=float(u.confidence),
        salience=float(u.salience),
        sensitivity=int(u.sensitivity),
        pin=int(u.pin),
        topic_tags=u.topic_tags,
        partner_affect_label=u.partner_affect_label,
        partner_affect_intensity=float(u.partner_affect_intensity) if u.partner_affect_intensity is not None else None,
    )


@router.get("/memories/{memory_id}/units", response_model=UnitListResponse)
def list_units(
    memory_id: str,
    kind: Optional[int] = Query(default=None),
    state: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    config_store: ConfigStore = Depends(get_config_store_dep),
):
    """Unit一覧を返す（kind/stateで絞り込み可）。"""
    with memory_session_scope(memory_id, config_store.embedding_dimension) as db:
        q = db.query(Unit)
        if kind is not None:
            q = q.filter(Unit.kind == int(kind))
        if state is not None:
            q = q.filter(Unit.state == int(state))
        units = q.order_by(Unit.created_at.desc(), Unit.id.desc()).offset(offset).limit(limit).all()
        return UnitListResponse(items=[_to_unit_meta(u) for u in units])


@router.get("/memories/{memory_id}/units/{unit_id}", response_model=UnitDetailResponse)
def get_unit(
    memory_id: str,
    unit_id: int,
    config_store: ConfigStore = Depends(get_config_store_dep),
):
    """Unit詳細（メタ + kindに応じたpayload）を返す。"""
    with memory_session_scope(memory_id, config_store.embedding_dimension) as db:
        unit = db.query(Unit).filter(Unit.id == unit_id).one_or_none()
        if unit is None:
            raise HTTPException(status_code=404, detail="unit not found")

        payload: Dict[str, Any] = {}
        if unit.kind == int(UnitKind.EPISODE):
            pe = db.query(PayloadEpisode).filter(PayloadEpisode.unit_id == unit_id).one_or_none()
            if pe:
                payload = {
                    "user_text": pe.user_text,
                    "reply_text": pe.reply_text,
                    "image_summary": pe.image_summary,
                    "context_note": pe.context_note,
                    "reflection_json": pe.reflection_json,
                }
        elif unit.kind == int(UnitKind.FACT):
            pf = db.query(PayloadFact).filter(PayloadFact.unit_id == unit_id).one_or_none()
            if pf:
                payload = {
                    "subject_entity_id": pf.subject_entity_id,
                    "predicate": pf.predicate,
                    "object_text": pf.object_text,
                    "object_entity_id": pf.object_entity_id,
                    "valid_from": pf.valid_from,
                    "valid_to": pf.valid_to,
                    "evidence_unit_ids_json": pf.evidence_unit_ids_json,
                }
        elif unit.kind == int(UnitKind.SUMMARY):
            ps = db.query(PayloadSummary).filter(PayloadSummary.unit_id == unit_id).one_or_none()
            if ps:
                payload = {
                    "scope_label": ps.scope_label,
                    "scope_key": ps.scope_key,
                    "range_start": ps.range_start,
                    "range_end": ps.range_end,
                    "summary_text": ps.summary_text,
                    "summary_json": ps.summary_json,
                }
        elif unit.kind == int(UnitKind.CAPSULE):
            cap = db.query(PayloadCapsule).filter(PayloadCapsule.unit_id == unit_id).one_or_none()
            if cap:
                payload = {"expires_at": cap.expires_at, "capsule_json": cap.capsule_json}
        elif unit.kind == int(UnitKind.LOOP):
            pl = db.query(PayloadLoop).filter(PayloadLoop.unit_id == unit_id).one_or_none()
            if pl:
                payload = {"status": pl.status, "due_at": pl.due_at, "loop_text": pl.loop_text}

        return UnitDetailResponse(unit=_to_unit_meta(unit), payload=payload)


@router.patch("/memories/{memory_id}/units/{unit_id}", response_model=UnitMeta)
def update_unit(
    memory_id: str,
    unit_id: int,
    request: UnitUpdateRequest,
    config_store: ConfigStore = Depends(get_config_store_dep),
):
    """Unitのメタ情報（pin/sensitivity/state/topic_tags等）を更新する。"""
    now_ts = int(time.time())
    with memory_session_scope(memory_id, config_store.embedding_dimension) as db:
        unit = db.query(Unit).filter(Unit.id == unit_id).one_or_none()
        if unit is None:
            raise HTTPException(status_code=404, detail="unit not found")

        before = {
            "pin": int(unit.pin),
            "sensitivity": int(unit.sensitivity),
            "state": int(unit.state),
            "topic_tags": unit.topic_tags,
            "confidence": float(unit.confidence),
            "salience": float(unit.salience),
        }

        if request.pin is not None:
            unit.pin = int(request.pin)
        if request.sensitivity is not None:
            unit.sensitivity = int(request.sensitivity)
        if request.state is not None:
            unit.state = int(request.state)
        if request.topic_tags is not None:
            try:
                unit.topic_tags = canonicalize_topic_tags_json(request.topic_tags)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="topic_tags must be a JSON array string") from exc
        if request.confidence is not None:
            unit.confidence = float(request.confidence)
        if request.salience is not None:
            unit.salience = float(request.salience)

        unit.updated_at = now_ts
        db.add(unit)

        after = {
            "pin": int(unit.pin),
            "sensitivity": int(unit.sensitivity),
            "state": int(unit.state),
            "topic_tags": unit.topic_tags,
            "confidence": float(unit.confidence),
            "salience": float(unit.salience),
        }
        if after != before:
            record_unit_version(
                db,
                unit_id=int(unit.id),
                payload_obj=after,
                patch_reason="admin_update_unit_meta",
                now_ts=now_ts,
            )
            sync_unit_vector_metadata(
                db,
                unit_id=int(unit.id),
                occurred_at=unit.occurred_at,
                state=int(unit.state),
                sensitivity=int(unit.sensitivity),
            )
        return _to_unit_meta(unit)
