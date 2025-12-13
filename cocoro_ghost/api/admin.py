"""管理API（Unit閲覧・編集、persona/contract設定）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope, sync_unit_vector_metadata
from cocoro_ghost.deps import get_config_store_dep
import json
import time

from cocoro_ghost.schemas import (
    ContractUpsertRequest,
    PersonaUpsertRequest,
    UnitDetailResponse,
    UnitListResponse,
    UnitMeta,
    UnitUpdateRequest,
    WeeklySummaryEnqueueRequest,
)
from cocoro_ghost.unit_enums import JobStatus, UnitKind
from cocoro_ghost.unit_models import (
    Job,
    PayloadCapsule,
    PayloadContract,
    PayloadEpisode,
    PayloadFact,
    PayloadLoop,
    PayloadPersona,
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
        emotion_label=u.emotion_label,
        emotion_intensity=float(u.emotion_intensity) if u.emotion_intensity is not None else None,
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
                    "scope_type": ps.scope_type,
                    "scope_key": ps.scope_key,
                    "range_start": ps.range_start,
                    "range_end": ps.range_end,
                    "summary_text": ps.summary_text,
                }
        elif unit.kind == int(UnitKind.PERSONA):
            pp = db.query(PayloadPersona).filter(PayloadPersona.unit_id == unit_id).one_or_none()
            if pp:
                payload = {"persona_text": pp.persona_text, "is_active": pp.is_active}
        elif unit.kind == int(UnitKind.CONTRACT):
            pc = db.query(PayloadContract).filter(PayloadContract.unit_id == unit_id).one_or_none()
            if pc:
                payload = {"contract_text": pc.contract_text, "is_active": pc.is_active}
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
            unit.topic_tags = request.topic_tags
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


@router.post("/memories/{memory_id}/persona")
def set_persona(
    memory_id: str,
    request: PersonaUpsertRequest,
    config_store: ConfigStore = Depends(get_config_store_dep),
):
    now_ts = int(time.time())
    with memory_session_scope(memory_id, config_store.embedding_dimension) as db:
        if request.set_active:
            db.query(PayloadPersona).update({PayloadPersona.is_active: 0})

        unit = Unit(
            kind=int(UnitKind.PERSONA),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source="admin",
            state=0,
            confidence=0.5,
            salience=0.0,
            sensitivity=int(request.sensitivity),
            pin=1,
        )
        db.add(unit)
        db.flush()
        is_active = 1 if request.set_active else 0
        db.add(PayloadPersona(unit_id=unit.id, persona_text=request.persona_text, is_active=is_active))
        record_unit_version(
            db,
            unit_id=int(unit.id),
            payload_obj={"persona_text": request.persona_text, "is_active": is_active},
            patch_reason="admin_set_persona",
            now_ts=now_ts,
        )
        return {"unit_id": int(unit.id)}


@router.post("/memories/{memory_id}/contract")
def set_contract(
    memory_id: str,
    request: ContractUpsertRequest,
    config_store: ConfigStore = Depends(get_config_store_dep),
):
    now_ts = int(time.time())
    with memory_session_scope(memory_id, config_store.embedding_dimension) as db:
        if request.set_active:
            db.query(PayloadContract).update({PayloadContract.is_active: 0})

        unit = Unit(
            kind=int(UnitKind.CONTRACT),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source="admin",
            state=0,
            confidence=0.5,
            salience=0.0,
            sensitivity=int(request.sensitivity),
            pin=1,
        )
        db.add(unit)
        db.flush()
        is_active = 1 if request.set_active else 0
        db.add(
            PayloadContract(
                unit_id=unit.id,
                contract_text=request.contract_text,
                is_active=is_active,
            )
        )
        record_unit_version(
            db,
            unit_id=int(unit.id),
            payload_obj={"contract_text": request.contract_text, "is_active": is_active},
            patch_reason="admin_set_contract",
            now_ts=now_ts,
        )
        return {"unit_id": int(unit.id)}


@router.post("/memories/{memory_id}/jobs/weekly_summary")
def enqueue_weekly_summary(
    memory_id: str,
    request: WeeklySummaryEnqueueRequest,
    config_store: ConfigStore = Depends(get_config_store_dep),
):
    now_ts = int(time.time())
    payload: Dict[str, Any] = {}
    if request.week_key:
        payload["week_key"] = request.week_key

    with memory_session_scope(memory_id, config_store.embedding_dimension) as db:
        job = Job(
            kind="weekly_summary",
            payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            status=int(JobStatus.QUEUED),
            run_after=now_ts,
            tries=0,
            last_error=None,
            created_at=now_ts,
            updated_at=now_ts,
        )
        db.add(job)
        db.flush()
        return {"job_id": int(job.id)}
