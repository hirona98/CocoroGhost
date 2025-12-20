"""非同期ジョブWorker（jobsテーブル実行）。"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from cocoro_ghost import prompts
from cocoro_ghost.db import get_memory_session, sync_unit_vector_metadata, upsert_unit_vector
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.unit_enums import (
    EntityRole,
    EntityType,
    JobStatus,
    LoopStatus,
    RelationType,
    Sensitivity,
    SummaryScopeType,
    UnitKind,
    UnitState,
)
from cocoro_ghost.unit_models import (
    Edge,
    Entity,
    EntityAlias,
    Job,
    PayloadCapsule,
    PayloadEpisode,
    PayloadFact,
    PayloadLoop,
    PayloadSummary,
    Unit,
    UnitEntity,
)
from cocoro_ghost.versioning import canonical_json_dumps, record_unit_version
from cocoro_ghost.topic_tags import canonicalize_topic_tags, dumps_topic_tags_json


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


def _parse_optional_epoch_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except Exception:  # noqa: BLE001
            pass
        try:
            # ISO 8601 (e.g. "2025-12-13T00:00:00Z")
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:  # noqa: BLE001
            return None
    return None


def _backoff_seconds(tries: int) -> int:
    return min(3600, max(5, 2 ** max(0, tries)))


def _has_pending_job_with_payload(session: Session, *, kind: str, payload_key: str, payload_value: Any) -> bool:
    """同種ジョブの重複enqueueを避けるための簡易判定（queued/runningのみ）。"""
    rows = (
        session.query(Job)
        .filter(Job.kind == kind, Job.status.in_([int(JobStatus.QUEUED), int(JobStatus.RUNNING)]))
        .order_by(Job.id.desc())
        .limit(200)
        .all()
    )
    for job in rows:
        payload = _json_loads(job.payload_json)
        if payload.get(payload_key) == payload_value:
            return True
    return False


def _enqueue_job(session: Session, *, kind: str, payload: Dict[str, Any], now_ts: int) -> None:
    session.add(
        Job(
            kind=kind,
            payload_json=_json_dumps(payload),
            status=int(JobStatus.QUEUED),
            run_after=now_ts,
            tries=0,
            last_error=None,
            created_at=now_ts,
            updated_at=now_ts,
        )
    )


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
            _handle_extract_entities(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "weekly_summary":
            _handle_weekly_summary(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "person_summary_refresh":
            _handle_person_summary_refresh(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "topic_summary_refresh":
            _handle_topic_summary_refresh(session=session, llm_client=llm_client, payload=payload, now_ts=now_ts)
        elif job.kind == "capsule_refresh":
            _handle_capsule_refresh(session=session, payload=payload, now_ts=now_ts)
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
    periodic_interval_seconds: float = 30.0,
    stop_event: threading.Event | None = None,
) -> None:
    logger.info("worker start", extra={"memory_id": memory_id})
    last_periodic_at: float = 0.0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        # cron無し運用: Workerプロセス内で定期enqueueを行う（軽量）。
        if periodic_interval_seconds > 0:
            now_s = time.time()
            if (now_s - last_periodic_at) >= float(periodic_interval_seconds):
                last_periodic_at = now_s
                session = get_memory_session(memory_id, embedding_dimension)
                try:
                    from cocoro_ghost.periodic import enqueue_periodic_jobs

                    stats = enqueue_periodic_jobs(session, now_ts=int(now_s))
                    session.commit()
                    if any(int(v) > 0 for v in stats.values()):
                        logger.info("periodic enqueued", extra={"memory_id": memory_id, **stats})
                except Exception:  # noqa: BLE001
                    session.rollback()
                    logger.exception("periodic enqueue failed", extra={"memory_id": memory_id})
                finally:
                    session.close()

        processed = process_due_jobs(
            memory_id=memory_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            max_jobs=max_jobs_per_tick,
            sleep_when_empty=0.0,
        )
        if processed <= 0:
            if stop_event is not None:
                stop_event.wait(poll_interval_seconds)
            else:
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
    topic_tags_raw = data.get("topic_tags")
    topic_tags = topic_tags_raw if isinstance(topic_tags_raw, list) else []
    canonical_tags = canonicalize_topic_tags(topic_tags)
    data["topic_tags"] = canonical_tags
    unit.topic_tags = dumps_topic_tags_json(canonical_tags)
    unit.state = int(UnitState.VALIDATED)
    unit.updated_at = now_ts
    pe.reflection_json = raw_json
    session.add(unit)
    session.add(pe)
    sync_unit_vector_metadata(
        session,
        unit_id=unit_id,
        occurred_at=unit.occurred_at,
        state=int(unit.state),
        sensitivity=int(unit.sensitivity),
    )
    record_unit_version(
        session,
        unit_id=unit_id,
        payload_obj=data,
        patch_reason="reflect_episode",
        now_ts=now_ts,
    )


def _normalize_entity_name(name: str) -> str:
    return (name or "").strip().lower()


def _etype_to_int(etype_raw: str) -> Optional[int]:
    key = (etype_raw or "").strip().upper()
    try:
        return int(EntityType[key])
    except Exception:  # noqa: BLE001
        return None


def _parse_entity_ref(ref: str) -> Optional[tuple[int, str]]:
    s = (ref or "").strip()
    if ":" not in s:
        return None
    etype_raw, name = s.split(":", 1)
    etype = _etype_to_int(etype_raw)
    if etype is None:
        return None
    name = name.strip()
    if not name:
        return None
    return etype, name


def _relation_to_int(rel_raw: str) -> int:
    key = (rel_raw or "").strip().lower()
    mapping = {
        "friend": RelationType.FRIEND,
        "family": RelationType.FAMILY,
        "colleague": RelationType.COLLEAGUE,
        "partner": RelationType.PARTNER,
        "likes": RelationType.LIKES,
        "like": RelationType.LIKES,
        "dislikes": RelationType.DISLIKES,
        "dislike": RelationType.DISLIKES,
        "related": RelationType.RELATED,
        "other": RelationType.OTHER,
    }
    return int(mapping.get(key, RelationType.OTHER))


def _get_or_create_entity(session: Session, *, etype: int, name: str, aliases: list[str], now_ts: int) -> Entity:
    normalized = _normalize_entity_name(name)
    ent = (
        session.query(Entity)
        .filter(Entity.etype == etype, Entity.normalized == normalized)
        .order_by(Entity.id.asc())
        .first()
    )
    if ent is None:
        ent = Entity(etype=etype, name=name, normalized=normalized, created_at=now_ts, updated_at=now_ts)
        session.add(ent)
        session.flush()
    else:
        if ent.name != name:
            ent.name = name
        ent.updated_at = now_ts
        session.add(ent)

    for a in aliases:
        a = (a or "").strip()
        if not a:
            continue
        session.merge(EntityAlias(entity_id=int(ent.id), alias=a))

    return ent


def _handle_extract_entities(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    unit_id = int(payload["unit_id"])
    row = (
        session.query(Unit, PayloadEpisode)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(Unit.id == unit_id, Unit.kind == int(UnitKind.EPISODE))
        .one_or_none()
    )
    if row is None:
        return
    _u, pe = row
    text_in = "\n".join(filter(None, [pe.user_text, pe.reply_text, pe.image_summary]))
    if not text_in.strip():
        return

    resp = llm_client.generate_json_response(system_prompt=prompts.get_entity_extract_prompt(), user_text=text_in)
    data = json.loads(llm_client.response_content(resp))
    entities = data.get("entities") or []
    if not isinstance(entities, list):
        return

    for e in entities:
        if not isinstance(e, dict):
            continue
        etype_raw = str(e.get("etype") or "")
        etype = _etype_to_int(etype_raw)
        if etype is None:
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        aliases_raw = e.get("aliases") or []
        if not isinstance(aliases_raw, list):
            aliases_raw = []
        aliases = [str(a) for a in aliases_raw if str(a).strip()]
        confidence = float(e.get("confidence") or 0.0)
        ent = _get_or_create_entity(session, etype=etype, name=name, aliases=aliases, now_ts=now_ts)
        session.merge(
            UnitEntity(
                unit_id=unit_id,
                entity_id=int(ent.id),
                role=int(EntityRole.MENTIONED),
                weight=max(0.1, confidence) if confidence > 0 else 1.0,
            )
        )

    # Entity抽出の結果をもとに、人物/トピックのサマリ更新をenqueueする（ベストエフォート）。
    # - 会話の一貫性（継続性）に効くが、毎回大量に更新するとコストが高いので上限を設ける。
    try:
        ue_rows = (
            session.query(UnitEntity.entity_id, UnitEntity.weight)
            .filter(UnitEntity.unit_id == unit_id)
            .order_by(UnitEntity.weight.desc())
            .limit(12)
            .all()
        )
        entity_ids = [int(eid) for eid, _w in ue_rows]
        if entity_ids:
            ent_rows = session.query(Entity.id, Entity.etype).filter(Entity.id.in_(entity_ids)).all()
            etype_by_id = {int(eid): int(etype) for eid, etype in ent_rows}

            # 直近の会話に重要そうな上位エンティティのみ更新対象にする。
            person_ids: list[int] = []
            topic_ids: list[int] = []
            for eid, _w in ue_rows:
                eid_i = int(eid)
                etype = etype_by_id.get(eid_i)
                if etype == int(EntityType.PERSON):
                    person_ids.append(eid_i)
                elif etype == int(EntityType.TOPIC):
                    topic_ids.append(eid_i)

            # 上限（過剰enqueue防止）
            person_ids = person_ids[:3]
            topic_ids = topic_ids[:3]

            for person_id in person_ids:
                if _has_pending_job_with_payload(
                    session,
                    kind="person_summary_refresh",
                    payload_key="entity_id",
                    payload_value=int(person_id),
                ):
                    continue
                _enqueue_job(session, kind="person_summary_refresh", payload={"entity_id": int(person_id)}, now_ts=now_ts)

            for topic_id in topic_ids:
                if _has_pending_job_with_payload(
                    session,
                    kind="topic_summary_refresh",
                    payload_key="entity_id",
                    payload_value=int(topic_id),
                ):
                    continue
                _enqueue_job(session, kind="topic_summary_refresh", payload={"entity_id": int(topic_id)}, now_ts=now_ts)
    except Exception:  # noqa: BLE001
        logger.debug("failed to enqueue person/topic summary refresh", exc_info=True)

    relations = data.get("relations") or []
    if not isinstance(relations, list):
        return
    for r in relations:
        if not isinstance(r, dict):
            continue
        src_raw = str(r.get("src") or "")
        dst_raw = str(r.get("dst") or "")
        rel_raw = str(r.get("rel") or "")
        if not src_raw.strip() or not dst_raw.strip() or not rel_raw.strip():
            continue

        src_parsed = _parse_entity_ref(src_raw)
        dst_parsed = _parse_entity_ref(dst_raw)
        if src_parsed is None or dst_parsed is None:
            continue
        src_type, src_name = src_parsed
        dst_type, dst_name = dst_parsed
        rel_type = _relation_to_int(rel_raw)

        confidence = float(r.get("confidence") or 0.0)
        weight = max(0.1, confidence) if confidence > 0 else 1.0

        src_ent = _get_or_create_entity(session, etype=src_type, name=src_name, aliases=[], now_ts=now_ts)
        dst_ent = _get_or_create_entity(session, etype=dst_type, name=dst_name, aliases=[], now_ts=now_ts)

        edge = (
            session.query(Edge)
            .filter(
                Edge.src_entity_id == int(src_ent.id),
                Edge.rel_type == int(rel_type),
                Edge.dst_entity_id == int(dst_ent.id),
            )
            .one_or_none()
        )
        if edge is None:
            session.add(
                Edge(
                    src_entity_id=int(src_ent.id),
                    rel_type=int(rel_type),
                    dst_entity_id=int(dst_ent.id),
                    weight=weight,
                    first_seen_at=now_ts,
                    last_seen_at=now_ts,
                    evidence_unit_id=unit_id,
                )
            )
        else:
            edge.weight = max(float(edge.weight or 0.0), weight)
            edge.last_seen_at = now_ts
            if edge.first_seen_at is None:
                edge.first_seen_at = now_ts
            edge.evidence_unit_id = unit_id
            session.add(edge)


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
    elif unit.kind == int(UnitKind.SUMMARY):
        ps = session.query(PayloadSummary).filter(PayloadSummary.unit_id == unit_id).one_or_none()
        if ps is None:
            return
        text_to_embed = ps.summary_text
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


def _find_existing_fact_unit_id(
    session: Session,
    *,
    subject_entity_id: Optional[int],
    predicate: str,
    object_text: Optional[str],
    evidence_unit_id: int,
) -> Optional[int]:
    row = session.execute(
        text(
            """
            SELECT pf.unit_id
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
    return int(row[0]) if row is not None else None


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

        validity = f.get("validity")
        valid_from = None
        valid_to = None
        if isinstance(validity, dict):
            valid_from = _parse_optional_epoch_seconds(validity.get("from"))
            valid_to = _parse_optional_epoch_seconds(validity.get("to"))

        subject_entity_id: Optional[int] = None
        subj = f.get("subject")
        subj_name = "USER"
        subj_etype_raw = "PERSON"
        if isinstance(subj, dict):
            subj_name = str(subj.get("name") or "").strip() or "USER"
            subj_etype_raw = str(subj.get("etype") or "").strip() or "PERSON"

        if subj_name.strip().upper() == "USER":
            user_ent = _get_or_create_entity(session, etype=int(EntityType.PERSON), name="USER", aliases=["USER"], now_ts=now_ts)
            subject_entity_id = int(user_ent.id)
        else:
            subj_etype = _etype_to_int(subj_etype_raw) or int(EntityType.PERSON)
            ent = _get_or_create_entity(session, etype=subj_etype, name=subj_name, aliases=[], now_ts=now_ts)
            subject_entity_id = int(ent.id)

        existing_fact_unit_id = _find_existing_fact_unit_id(
            session,
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            object_text=obj_text,
            evidence_unit_id=unit_id,
        )
        if existing_fact_unit_id is not None:
            existing = (
                session.query(Unit, PayloadFact)
                .join(PayloadFact, PayloadFact.unit_id == Unit.id)
                .filter(Unit.id == int(existing_fact_unit_id), Unit.kind == int(UnitKind.FACT))
                .one_or_none()
            )
            if existing is None:
                continue
            fact_unit, pf = existing
            changed = False
            if valid_from is not None and pf.valid_from != valid_from:
                pf.valid_from = valid_from
                changed = True
            if valid_to is not None and pf.valid_to != valid_to:
                pf.valid_to = valid_to
                changed = True
            if confidence and float(fact_unit.confidence or 0.0) != float(confidence):
                fact_unit.confidence = float(confidence)
                changed = True
            if changed:
                fact_unit.updated_at = now_ts
                session.add(fact_unit)
                session.add(pf)
                record_unit_version(
                    session,
                    unit_id=int(fact_unit.id),
                    payload_obj={
                        "subject_entity_id": pf.subject_entity_id,
                        "predicate": pf.predicate,
                        "object_text": pf.object_text,
                        "object_entity_id": pf.object_entity_id,
                        "valid_from": pf.valid_from,
                        "valid_to": pf.valid_to,
                        "evidence_unit_ids": json.loads(pf.evidence_unit_ids_json or "[]"),
                    },
                    patch_reason="extract_facts_update",
                    now_ts=now_ts,
                )
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
        fact_payload = {
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
            "object_text": obj_text,
            "object_entity_id": None,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "evidence_unit_ids": [unit_id],
        }
        session.add(
            PayloadFact(
                unit_id=fact_unit.id,
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                object_text=obj_text,
                object_entity_id=None,
                valid_from=valid_from,
                valid_to=valid_to,
                evidence_unit_ids_json=_json_dumps([unit_id]),
            )
        )
        record_unit_version(
            session,
            unit_id=int(fact_unit.id),
            payload_obj=fact_payload,
            patch_reason="extract_facts",
            now_ts=now_ts,
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
        status_raw = str(l.get("status") or "open").strip().lower()
        status = int(LoopStatus.CLOSED) if status_raw in ("closed", "close") else int(LoopStatus.OPEN)
        due_at = _parse_optional_epoch_seconds(l.get("due_at"))

        if status == int(LoopStatus.CLOSED):
            existing = (
                session.query(Unit, PayloadLoop)
                .join(PayloadLoop, PayloadLoop.unit_id == Unit.id)
                .filter(
                    Unit.kind == int(UnitKind.LOOP),
                    PayloadLoop.status == int(LoopStatus.OPEN),
                    PayloadLoop.loop_text == loop_text,
                )
                .order_by(Unit.created_at.desc(), Unit.id.desc())
                .first()
            )
            if existing is not None:
                unit, pl = existing
                pl.status = int(LoopStatus.CLOSED)
                if due_at is not None:
                    pl.due_at = due_at
                unit.updated_at = now_ts
                session.add(unit)
                session.add(pl)
                record_unit_version(
                    session,
                    unit_id=int(unit.id),
                    payload_obj={"status": int(pl.status), "due_at": pl.due_at, "loop_text": pl.loop_text},
                    patch_reason="extract_loops_close",
                    now_ts=now_ts,
                )
            continue

        if _loop_exists(session, loop_text=loop_text):
            existing = (
                session.query(Unit, PayloadLoop)
                .join(PayloadLoop, PayloadLoop.unit_id == Unit.id)
                .filter(
                    Unit.kind == int(UnitKind.LOOP),
                    PayloadLoop.status == int(LoopStatus.OPEN),
                    PayloadLoop.loop_text == loop_text,
                )
                .order_by(Unit.created_at.desc(), Unit.id.desc())
                .first()
            )
            if existing is not None:
                unit, pl = existing
                changed = False
                if due_at is not None and pl.due_at != due_at:
                    pl.due_at = due_at
                    changed = True
                if float(l.get("confidence") or 0.0) and float(unit.confidence or 0.0) != float(l.get("confidence") or 0.0):
                    unit.confidence = float(l.get("confidence") or 0.0)
                    changed = True
                if changed:
                    unit.updated_at = now_ts
                    session.add(unit)
                    session.add(pl)
                    record_unit_version(
                        session,
                        unit_id=int(unit.id),
                        payload_obj={"status": int(pl.status), "due_at": pl.due_at, "loop_text": pl.loop_text},
                        patch_reason="extract_loops_update",
                        now_ts=now_ts,
                    )
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
        loop_payload = {
            "status": int(LoopStatus.OPEN),
            "due_at": due_at,
            "loop_text": loop_text,
        }
        session.add(
            PayloadLoop(
                unit_id=pl_unit.id,
                status=int(LoopStatus.OPEN),
                due_at=due_at,
                loop_text=loop_text,
            )
        )
        for ue in session.query(UnitEntity).filter(UnitEntity.unit_id == unit_id).all():
            session.merge(
                UnitEntity(
                    unit_id=int(pl_unit.id),
                    entity_id=int(ue.entity_id),
                    role=int(ue.role),
                    weight=float(ue.weight),
                )
            )
        record_unit_version(
            session,
            unit_id=int(pl_unit.id),
            payload_obj=loop_payload,
            patch_reason="extract_loops",
            now_ts=now_ts,
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


def _handle_capsule_refresh(*, session: Session, payload: Dict[str, Any], now_ts: int) -> None:
    """短期状態（Capsule）を更新する（LLM不要・軽量）。"""
    limit = int(payload.get("limit") or 5)
    limit = max(1, min(20, limit))

    rows = (
        session.query(Unit, PayloadEpisode)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(Unit.kind == int(UnitKind.EPISODE), Unit.state.in_([0, 1, 2]), Unit.sensitivity <= int(Sensitivity.PRIVATE))
        .order_by(Unit.created_at.desc(), Unit.id.desc())
        .limit(limit)
        .all()
    )

    recent = []
    for u, pe in rows:
        recent.append(
            {
                "unit_id": int(u.id),
                "occurred_at": int(u.occurred_at) if u.occurred_at is not None else None,
                "source": u.source,
                "user_text": (pe.user_text or "")[:200],
                "reply_text": (pe.reply_text or "")[:200],
                "topic_tags": u.topic_tags,
                "emotion_label": u.emotion_label,
            }
        )

    capsule_obj = {
        "generated_at": now_ts,
        "window": limit,
        "recent": recent,
    }
    capsule_json = _json_dumps(capsule_obj)
    expires_at = now_ts + 3600

    existing = (
        session.query(Unit, PayloadCapsule)
        .join(PayloadCapsule, PayloadCapsule.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.CAPSULE),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= int(Sensitivity.PRIVATE),
            PayloadCapsule.expires_at.isnot(None),
            PayloadCapsule.expires_at > now_ts,
        )
        .order_by(Unit.created_at.desc(), Unit.id.desc())
        .first()
    )

    if existing is None:
        cap_unit = Unit(
            kind=int(UnitKind.CAPSULE),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source="capsule_refresh",
            state=int(UnitState.VALIDATED),
            confidence=0.5,
            salience=0.0,
            sensitivity=int(Sensitivity.PRIVATE),
            pin=0,
            topic_tags=None,
            emotion_label=None,
            emotion_intensity=None,
        )
        session.add(cap_unit)
        session.flush()
        session.add(PayloadCapsule(unit_id=cap_unit.id, expires_at=expires_at, capsule_json=capsule_json))
        record_unit_version(
            session,
            unit_id=int(cap_unit.id),
            payload_obj={"expires_at": expires_at, "capsule": capsule_obj},
            patch_reason="capsule_refresh",
            now_ts=now_ts,
        )
        return

    cap_unit, cap = existing
    cap.expires_at = expires_at
    cap.capsule_json = capsule_json
    cap_unit.updated_at = now_ts
    session.add(cap_unit)
    session.add(cap)
    record_unit_version(
        session,
        unit_id=int(cap_unit.id),
        payload_obj={"expires_at": expires_at, "capsule": capsule_obj},
        patch_reason="capsule_refresh",
        now_ts=now_ts,
    )


def _parse_week_key(week_key: str) -> tuple[int, int]:
    # "YYYY-Www"
    s = (week_key or "").strip()
    if "-W" not in s:
        raise ValueError("invalid week_key")
    year_s, week_s = s.split("-W", 1)
    year = int(year_s)
    week = int(week_s)
    start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc, hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return int(start.timestamp()), int(end.timestamp())


def _utc_week_key(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _handle_weekly_summary(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    week_key = str(payload.get("week_key") or "").strip() or _utc_week_key(now_ts)
    range_start, range_end = _parse_week_key(week_key)

    ep_rows = (
        session.query(Unit, PayloadEpisode)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.EPISODE),
            Unit.occurred_at.isnot(None),
            Unit.occurred_at >= range_start,
            Unit.occurred_at < range_end,
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= 1,
        )
        .order_by(Unit.occurred_at.asc(), Unit.id.asc())
        .limit(200)
        .all()
    )

    lines = []
    for u, pe in ep_rows:
        ut = (pe.user_text or "").strip().replace("\n", " ")
        rt = (pe.reply_text or "").strip().replace("\n", " ")
        if not ut and not rt:
            continue
        ut = ut[:200]
        rt = rt[:220]
        lines.append(f"- unit_id={int(u.id)} user='{ut}' reply='{rt}'")

    input_text = f"week_key: {week_key}\n\n[EPISODES]\n" + "\n".join(lines)

    resp = llm_client.generate_json_response(system_prompt=prompts.get_weekly_summary_prompt(), user_text=input_text)
    data = json.loads(llm_client.response_content(resp))
    summary_text = str(data.get("summary_text") or "").strip()
    if not summary_text:
        return
    key_events_raw = data.get("key_events") or []
    key_events: list[dict[str, Any]] = []
    if isinstance(key_events_raw, list):
        for item in key_events_raw:
            if not isinstance(item, dict):
                continue
            why = str(item.get("why") or "").strip()
            try:
                unit_id = int(item.get("unit_id"))
            except Exception:  # noqa: BLE001
                continue
            if not why:
                continue
            key_events.append({"unit_id": unit_id, "why": why})
    key_events = key_events[:5]

    relationship_state = str(data.get("relationship_state") or "").strip()
    summary_obj = {
        "summary_text": summary_text,
        "key_events": key_events,
        "relationship_state": relationship_state,
    }
    summary_json = canonical_json_dumps(summary_obj)

    existing = (
        session.query(Unit, PayloadSummary)
        .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.SUMMARY),
            PayloadSummary.scope_type == int(SummaryScopeType.RELATIONSHIP),
            PayloadSummary.scope_key == week_key,
        )
        .order_by(Unit.created_at.desc())
        .first()
    )

    payload_obj = {
        "scope_type": int(SummaryScopeType.RELATIONSHIP),
        "scope_key": week_key,
        "range_start": range_start,
        "range_end": range_end,
        "summary": summary_obj,
    }

    if existing is None:
        unit = Unit(
            kind=int(UnitKind.SUMMARY),
            occurred_at=range_end - 1,
            created_at=now_ts,
            updated_at=now_ts,
            source="weekly_summary",
            state=int(UnitState.RAW),
            confidence=0.5,
            salience=0.0,
            sensitivity=0,
            pin=0,
        )
        session.add(unit)
        session.flush()
        session.add(
            PayloadSummary(
                unit_id=unit.id,
                scope_type=int(SummaryScopeType.RELATIONSHIP),
                scope_key=week_key,
                range_start=range_start,
                range_end=range_end,
                summary_text=summary_text,
                summary_json=summary_json,
            )
        )
        record_unit_version(
            session,
            unit_id=int(unit.id),
            payload_obj=payload_obj,
            patch_reason="weekly_summary",
            now_ts=now_ts,
        )
        session.add(
            Job(
                kind="upsert_embeddings",
                payload_json=_json_dumps({"unit_id": int(unit.id)}),
                status=int(JobStatus.QUEUED),
                run_after=now_ts,
                tries=0,
                last_error=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
        )
        return

    unit, ps = existing
    before_text = ps.summary_text
    ps.summary_text = summary_text
    ps.summary_json = summary_json
    ps.range_start = range_start
    ps.range_end = range_end
    unit.updated_at = now_ts
    unit.state = int(UnitState.VALIDATED)
    session.add(unit)
    session.add(ps)
    record_unit_version(
        session,
        unit_id=int(unit.id),
        payload_obj=payload_obj,
        patch_reason="weekly_summary",
        now_ts=now_ts,
    )
    sync_unit_vector_metadata(
        session,
        unit_id=int(unit.id),
        occurred_at=unit.occurred_at,
        state=int(unit.state),
        sensitivity=int(unit.sensitivity),
    )
    if before_text != summary_text:
        session.add(
            Job(
                kind="upsert_embeddings",
                payload_json=_json_dumps({"unit_id": int(unit.id)}),
                status=int(JobStatus.QUEUED),
                run_after=now_ts,
                tries=0,
                last_error=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
        )


def _build_summary_payload_input(*, header_lines: list[str], episode_lines: list[str]) -> str:
    parts = [*header_lines, "", "[EPISODES]", *episode_lines]
    return "\n".join([p for p in parts if p is not None]).strip()


def _enqueue_embeddings_if_changed(
    session: Session,
    *,
    unit: Unit,
    before_text: str | None,
    after_text: str,
    now_ts: int,
) -> None:
    if (before_text or "") == (after_text or ""):
        return
    _enqueue_job(session, kind="upsert_embeddings", payload={"unit_id": int(unit.id)}, now_ts=now_ts)


def _upsert_summary_unit(
    session: Session,
    *,
    scope_type: int,
    scope_key: str,
    range_start: int | None,
    range_end: int | None,
    summary_text: str,
    summary_json: str,
    now_ts: int,
    source: str,
    patch_reason: str,
) -> Unit | None:
    existing = (
        session.query(Unit, PayloadSummary)
        .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.SUMMARY),
            PayloadSummary.scope_type == int(scope_type),
            PayloadSummary.scope_key == str(scope_key),
        )
        .order_by(Unit.created_at.desc())
        .first()
    )

    if existing is None:
        unit = Unit(
            kind=int(UnitKind.SUMMARY),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source=source,
            state=int(UnitState.RAW),
            confidence=0.5,
            salience=0.0,
            sensitivity=0,
            pin=0,
        )
        session.add(unit)
        session.flush()
        session.add(
            PayloadSummary(
                unit_id=unit.id,
                scope_type=int(scope_type),
                scope_key=str(scope_key),
                range_start=range_start,
                range_end=range_end,
                summary_text=summary_text,
                summary_json=summary_json,
            )
        )
        record_unit_version(
            session,
            unit_id=int(unit.id),
            payload_obj={
                "scope_type": int(scope_type),
                "scope_key": str(scope_key),
                "range_start": range_start,
                "range_end": range_end,
                "summary_json": json.loads(summary_json),
            },
            patch_reason=patch_reason,
            now_ts=now_ts,
        )
        # 生成したサマリも検索対象にするため、埋め込みを更新する。
        _enqueue_job(session, kind="upsert_embeddings", payload={"unit_id": int(unit.id)}, now_ts=now_ts)
        return unit

    unit, ps = existing
    before_text = ps.summary_text
    ps.summary_text = summary_text
    ps.summary_json = summary_json
    ps.range_start = range_start
    ps.range_end = range_end
    unit.updated_at = now_ts
    unit.state = int(UnitState.VALIDATED)
    session.add(unit)
    session.add(ps)
    record_unit_version(
        session,
        unit_id=int(unit.id),
        payload_obj={
            "scope_type": int(scope_type),
            "scope_key": str(scope_key),
            "range_start": range_start,
            "range_end": range_end,
            "summary_json": json.loads(summary_json),
        },
        patch_reason=patch_reason,
        now_ts=now_ts,
    )
    sync_unit_vector_metadata(
        session,
        unit_id=int(unit.id),
        occurred_at=unit.occurred_at,
        state=int(unit.state),
        sensitivity=int(unit.sensitivity),
    )
    _enqueue_embeddings_if_changed(session, unit=unit, before_text=before_text, after_text=summary_text, now_ts=now_ts)
    return unit


def _handle_person_summary_refresh(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    """人物（EntityType.PERSON）に紐づく直近エピソードから要約を作成/更新する。"""
    entity_id = int(payload["entity_id"])
    ent = session.query(Entity).filter(Entity.id == entity_id).one_or_none()
    if ent is None or int(ent.etype) != int(EntityType.PERSON):
        return

    # 入力: 対象人物に紐づく直近エピソードを列挙（上限あり）。
    ep_rows = (
        session.query(Unit, PayloadEpisode)
        .join(UnitEntity, UnitEntity.unit_id == Unit.id)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.EPISODE),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= int(Sensitivity.PRIVATE),
            UnitEntity.entity_id == entity_id,
        )
        .order_by(Unit.occurred_at.desc().nulls_last(), Unit.id.desc())
        .limit(120)
        .all()
    )
    if not ep_rows:
        return

    lines: list[str] = []
    for u, pe in ep_rows:
        ut = (pe.user_text or "").strip().replace("\n", " ")[:220]
        rt = (pe.reply_text or "").strip().replace("\n", " ")[:240]
        if not ut and not rt:
            continue
        lines.append(f"- unit_id={int(u.id)} user='{ut}' reply='{rt}'")

    input_text = _build_summary_payload_input(
        header_lines=[f"scope: person", f"entity_id: {entity_id}", f"entity_name: {ent.name}"],
        episode_lines=lines,
    )

    resp = llm_client.generate_json_response(system_prompt=prompts.get_person_summary_prompt(), user_text=input_text)
    data = json.loads(llm_client.response_content(resp))
    summary_text = str(data.get("summary_text") or "").strip()
    if not summary_text:
        return

    key_events_raw = data.get("key_events") or []
    key_events: list[dict[str, Any]] = []
    if isinstance(key_events_raw, list):
        for item in key_events_raw[:5]:
            if not isinstance(item, dict):
                continue
            why = str(item.get("why") or "").strip()
            try:
                unit_id = int(item.get("unit_id"))
            except Exception:  # noqa: BLE001
                continue
            if why:
                key_events.append({"unit_id": unit_id, "why": why})

    summary_obj = {
        "summary_text": summary_text,
        "key_events": key_events,
        "notes": str(data.get("notes") or "").strip(),
    }
    summary_json = canonical_json_dumps(summary_obj)

    _upsert_summary_unit(
        session,
        scope_type=int(SummaryScopeType.PERSON),
        scope_key=f"person:{entity_id}",
        range_start=None,
        range_end=None,
        summary_text=summary_text,
        summary_json=summary_json,
        now_ts=now_ts,
        source="person_summary_refresh",
        patch_reason="person_summary_refresh",
    )


def _handle_topic_summary_refresh(*, session: Session, llm_client: LlmClient, payload: Dict[str, Any], now_ts: int) -> None:
    """トピック（EntityType.TOPIC）に紐づく直近エピソードから要約を作成/更新する。"""
    entity_id = int(payload["entity_id"])
    ent = session.query(Entity).filter(Entity.id == entity_id).one_or_none()
    if ent is None or int(ent.etype) != int(EntityType.TOPIC):
        return

    topic_key = str((ent.normalized or ent.name or "")).strip().lower()
    if not topic_key:
        return

    ep_rows = (
        session.query(Unit, PayloadEpisode)
        .join(UnitEntity, UnitEntity.unit_id == Unit.id)
        .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.EPISODE),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= int(Sensitivity.PRIVATE),
            UnitEntity.entity_id == entity_id,
        )
        .order_by(Unit.occurred_at.desc().nulls_last(), Unit.id.desc())
        .limit(120)
        .all()
    )
    if not ep_rows:
        return

    lines: list[str] = []
    for u, pe in ep_rows:
        ut = (pe.user_text or "").strip().replace("\n", " ")[:220]
        rt = (pe.reply_text or "").strip().replace("\n", " ")[:240]
        if not ut and not rt:
            continue
        lines.append(f"- unit_id={int(u.id)} user='{ut}' reply='{rt}'")

    input_text = _build_summary_payload_input(
        header_lines=[f"scope: topic", f"entity_id: {entity_id}", f"topic_key: {topic_key}", f"topic_name: {ent.name}"],
        episode_lines=lines,
    )

    resp = llm_client.generate_json_response(system_prompt=prompts.get_topic_summary_prompt(), user_text=input_text)
    data = json.loads(llm_client.response_content(resp))
    summary_text = str(data.get("summary_text") or "").strip()
    if not summary_text:
        return

    key_events_raw = data.get("key_events") or []
    key_events: list[dict[str, Any]] = []
    if isinstance(key_events_raw, list):
        for item in key_events_raw[:5]:
            if not isinstance(item, dict):
                continue
            why = str(item.get("why") or "").strip()
            try:
                unit_id = int(item.get("unit_id"))
            except Exception:  # noqa: BLE001
                continue
            if why:
                key_events.append({"unit_id": unit_id, "why": why})

    summary_obj = {
        "summary_text": summary_text,
        "key_events": key_events,
        "notes": str(data.get("notes") or "").strip(),
    }
    summary_json = canonical_json_dumps(summary_obj)

    _upsert_summary_unit(
        session,
        scope_type=int(SummaryScopeType.TOPIC),
        scope_key=f"topic:{topic_key}",
        range_start=None,
        range_end=None,
        summary_text=summary_text,
        summary_json=summary_json,
        now_ts=now_ts,
        source="topic_summary_refresh",
        patch_reason="topic_summary_refresh",
    )
