"""非同期ジョブWorker（jobsテーブル実行）。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from cocoro_ghost import prompts
from cocoro_ghost.db import get_memory_session, upsert_unit_vector
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.unit_enums import JobStatus, LoopStatus, UnitKind, UnitState
from cocoro_ghost.unit_models import Job, PayloadEpisode, PayloadFact, PayloadLoop, Unit


logger = logging.getLogger(__name__)


def _now_utc_ts() -> int:
    return int(time.time())


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_loads(payload_json: str) -> Dict[str, Any]:
    try:
        obj = json.loads(payload_json)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _backoff_seconds(tries: int) -> int:
    return min(3600, max(5, 2 ** max(0, tries)))


def claim_next_job(session: Session, *, now_ts: int) -> Optional[int]:
    job = (
        session.query(Job)
        .filter(Job.status == int(JobStatus.QUEUED), Job.run_after <= now_ts)
        .order_by(Job.run_after.asc(), Job.id.asc())
        .first()
    )
    if job is None:
        return None
    job.status = int(JobStatus.RUNNING)
    job.updated_at = now_ts
    session.add(job)
    session.commit()
    return int(job.id)


def process_job(
    *,
    session: Session,
    llm_client: LlmClient,
    job_id: int,
    now_ts: int,
    max_tries: int,
) -> bool:
    job = session.query(Job).filter(Job.id == job_id).one_or_none()
    if job is None:
        return False
    payload = _json_loads(job.payload_json)
    try:
        if job.kind == "reflect_episode":
            _handle_reflect_episode(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "upsert_embeddings":
            _handle_upsert_embeddings(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "extract_facts":
            _handle_extract_facts(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "extract_loops":
            _handle_extract_loops(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "extract_entities":
            # TODO: entities/edges の抽出は段階導入（まずはno-opで冪等）
            pass
        else:
            logger.warning("unknown job kind", extra={"job_id": job_id, "kind": job.kind})

        job.status = int(JobStatus.DONE)
        job.updated_at = now_ts
        session.add(job)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("job failed", exc_info=exc, extra={"job_id": job_id, "kind": job.kind})
        job.tries = int(job.tries or 0) + 1
        job.last_error = str(exc)
        job.updated_at = now_ts
        if job.tries >= max_tries:
            job.status = int(JobStatus.FAILED)
        else:
            job.status = int(JobStatus.QUEUED)
            job.run_after = now_ts + _backoff_seconds(job.tries)
        session.add(job)
        session.commit()
        return False


def process_due_jobs(
    *,
    memory_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    max_jobs: int = 10,
    max_tries: int = 5,
    sleep_when_empty: float = 0.0,
) -> int:
    processed = 0
    for _ in range(max_jobs):
        now_ts = _now_utc_ts()
        session = get_memory_session(memory_id, embedding_dimension)
        try:
            job_id = claim_next_job(session, now_ts=now_ts)
        finally:
            session.close()
        if job_id is None:
            if sleep_when_empty > 0:
                time.sleep(sleep_when_empty)
            break

        session = get_memory_session(memory_id, embedding_dimension)
        try:
            ok = process_job(session=session, llm_client=llm_client, job_id=job_id, now_ts=now_ts, max_tries=max_tries)
            if ok:
                processed += 1
        finally:
            session.close()

    return processed


def run_forever(
    *,
    memory_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    poll_interval_seconds: float = 1.0,
    max_jobs_per_tick: int = 10,
) -> None:
    logger.info("worker start", extra={"memory_id": memory_id})
    while True:
        processed = process_due_jobs(
            memory_id=memory_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            max_jobs=max_jobs_per_tick,
            sleep_when_empty=0.0,
        )
        if processed <= 0:
            time.sleep(poll_interval_seconds)


def _handle_reflect_episode(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    unit_id = int(payload["unit_id"])
    row = (
        session.query(Unit, PayloadEpisode)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(Unit.id == unit_id, Unit.kind == int(UnitKind.EPISODE))
        .one_or_none()
    )
    if row is None:
        return
    unit, pe = row
    ctx_parts = []
    if pe.user_text:
        ctx_parts.append(f"user: {pe.user_text}")
    if pe.reply_text:
        ctx_parts.append(f"reply: {pe.reply_text}")
    if pe.image_summary:
        ctx_parts.append(f"image_summary: {pe.image_summary}")
    if pe.context_note:
        ctx_parts.append(f"context_note: {pe.context_note}")
    context_text = "\n".join(ctx_parts)

    resp = llm_client.generate_json_response(system_prompt=prompts.get_reflection_prompt(), user_text=context_text)
    raw_text = llm_client.response_content(resp)
    raw_json = raw_text
    data = json.loads(raw_text)

    unit.emotion_label = str(data.get("emotion_label") or "")
    unit.emotion_intensity = float(data.get("emotion_intensity") or 0.0)
    unit.salience = float(data.get("salience_score") or 0.0)
    unit.confidence = float(data.get("confidence") or 0.5)
    topic_tags = data.get("topic_tags") or []
    unit.topic_tags = _json_dumps(topic_tags) if isinstance(topic_tags, list) else str(topic_tags)
    unit.state = int(UnitState.VALIDATED)
    unit.updated_at = now_ts
    pe.reflection_json = raw_json
    session.add(unit)
    session.add(pe)


def _handle_upsert_embeddings(
    *, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int
) -> None:
    unit_id = int(payload["unit_id"])
    unit = session.query(Unit).filter(Unit.id == unit_id).one_or_none()
    if unit is None:
        return

    text_to_embed = ""
    if unit.kind == int(UnitKind.EPISODE):
        pe = session.query(PayloadEpisode).filter(PayloadEpisode.unit_id == unit_id).one_or_none()
        if pe is None:
            return
        text_to_embed = "\n".join(filter(None, [pe.user_text, pe.reply_text, pe.image_summary]))
    elif unit.kind == int(UnitKind.FACT):
        pf = session.query(PayloadFact).filter(PayloadFact.unit_id == unit_id).one_or_none()
        if pf is None:
            return
        text_to_embed = "\n".join(filter(None, [str(pf.subject_entity_id), pf.predicate, pf.object_text]))
    elif unit.kind == int(UnitKind.LOOP):
        pl = session.query(PayloadLoop).filter(PayloadLoop.unit_id == unit_id).one_or_none()
        if pl is None:
            return
        text_to_embed = pl.loop_text
    else:
        return

    embedding = llm_client.generate_embedding([text_to_embed])[0]
    upsert_unit_vector(
        session,
        unit_id=unit_id,
        embedding=embedding,
        kind=int(unit.kind),
        occurred_at=unit.occurred_at,
        state=int(unit.state),
        sensitivity=int(unit.sensitivity),
    )


def _fact_exists(session: Session, *, subject_entity_id: Optional[int], predicate: str, object_text: Optional[str], evidence_unit_id: int) -> bool:
    row = session.execute(
        text(
            """
            SELECT 1
            FROM payload_fact pf
            JOIN units u ON u.id = pf.unit_id
            WHERE u.kind = :kind
              AND pf.subject_entity_id IS :subject_entity_id
              AND pf.predicate = :predicate
              AND pf.object_text IS :object_text
              AND EXISTS (
                SELECT 1 FROM json_each(pf.evidence_unit_ids_json) WHERE value = :evidence_unit_id
              )
            LIMIT 1
            """
        ),
        {
            "kind": int(UnitKind.FACT),
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
            "object_text": object_text,
            "evidence_unit_id": evidence_unit_id,
        },
    ).fetchone()
    return row is not None


def _handle_extract_facts(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    unit_id = int(payload["unit_id"])
    row = (
        session.query(Unit, PayloadEpisode)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(Unit.id == unit_id, Unit.kind == int(UnitKind.EPISODE))
        .one_or_none()
    )
    if row is None:
        return
    src_unit, pe = row
    text_in = "\n".join(filter(None, [pe.user_text, pe.reply_text, pe.image_summary]))
    if not text_in.strip():
        return

    resp = llm_client.generate_json_response(system_prompt=prompts.get_fact_extract_prompt(), user_text=text_in)
    data = json.loads(llm_client.response_content(resp))
    facts = data.get("facts") or []
    if not isinstance(facts, list):
        return

    for f in facts:
        if not isinstance(f, dict):
            continue
        predicate = str(f.get("predicate") or "").strip()
        obj_text = f.get("object_text")
        obj_text = str(obj_text).strip() if obj_text is not None else None
        confidence = float(f.get("confidence") or 0.0)
        if not predicate:
            continue

        subject_entity_id = None
        # NOTE: entity upsertは段階導入。現状はUSER固定(=NULL)で保存する。
        if _fact_exists(
            session,
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            object_text=obj_text,
            evidence_unit_id=unit_id,
        ):
            continue

        fact_unit = Unit(
            kind=int(UnitKind.FACT),
            occurred_at=src_unit.occurred_at,
            created_at=now_ts,
            updated_at=now_ts,
            source="extract_facts",
            state=int(UnitState.RAW),
            confidence=confidence,
            salience=0.0,
            sensitivity=int(src_unit.sensitivity),
            pin=0,
            topic_tags=None,
            emotion_label=None,
            emotion_intensity=None,
        )
        session.add(fact_unit)
        session.flush()
        session.add(
            PayloadFact(
                unit_id=fact_unit.id,
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                object_text=obj_text,
                object_entity_id=None,
                valid_from=None,
                valid_to=None,
                evidence_unit_ids_json=_json_dumps([unit_id]),
            )
        )
        session.add(
            Job(
                kind="upsert_embeddings",
                payload_json=_json_dumps({"unit_id": int(fact_unit.id)}),
                status=int(JobStatus.QUEUED),
                run_after=now_ts,
                tries=0,
                last_error=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
        )


def _loop_exists(session: Session, *, loop_text: str) -> bool:
    row = (
        session.query(PayloadLoop.unit_id)
        .join(Unit, Unit.id == PayloadLoop.unit_id)
        .filter(Unit.kind == int(UnitKind.LOOP), PayloadLoop.status == int(LoopStatus.OPEN), PayloadLoop.loop_text == loop_text)
        .limit(1)
        .scalar()
    )
    return row is not None


def _handle_extract_loops(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    unit_id = int(payload["unit_id"])
    row = (
        session.query(Unit, PayloadEpisode)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(Unit.id == unit_id, Unit.kind == int(UnitKind.EPISODE))
        .one_or_none()
    )
    if row is None:
        return
    src_unit, pe = row
    text_in = "\n".join(filter(None, [pe.user_text, pe.reply_text, pe.image_summary]))
    if not text_in.strip():
        return

    resp = llm_client.generate_json_response(system_prompt=prompts.get_loop_extract_prompt(), user_text=text_in)
    data = json.loads(llm_client.response_content(resp))
    loops = data.get("loops") or []
    if not isinstance(loops, list):
        return

    for l in loops:
        if not isinstance(l, dict):
            continue
        loop_text = str(l.get("loop_text") or "").strip()
        if not loop_text:
            continue
        if _loop_exists(session, loop_text=loop_text):
            continue

        pl_unit = Unit(
            kind=int(UnitKind.LOOP),
            occurred_at=src_unit.occurred_at,
            created_at=now_ts,
            updated_at=now_ts,
            source="extract_loops",
            state=int(UnitState.RAW),
            confidence=float(l.get("confidence") or 0.0),
            salience=0.0,
            sensitivity=int(src_unit.sensitivity),
            pin=0,
            topic_tags=None,
            emotion_label=None,
            emotion_intensity=None,
        )
        session.add(pl_unit)
        session.flush()
        session.add(
            PayloadLoop(
                unit_id=pl_unit.id,
                status=int(LoopStatus.OPEN),
                due_at=None,
                loop_text=loop_text,
            )
        )
        session.add(
            Job(
                kind="upsert_embeddings",
                payload_json=_json_dumps({"unit_id": int(pl_unit.id)}),
                status=int(JobStatus.QUEUED),
                run_after=now_ts,
                tries=0,
                last_error=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
        )

