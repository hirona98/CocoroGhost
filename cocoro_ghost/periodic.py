"""
cron無しの定期実行ユーティリティ

Workerから定期的に呼び出され、必要なジョブをenqueueする。
bond_summary、entity_summary、capsule_refreshなどの
定期ジョブを重複抑制・クールダウン付きで管理する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from cocoro_ghost.unit_enums import JobStatus, Sensitivity, UnitKind, UnitState
from cocoro_ghost.unit_models import Entity, Job, PayloadEpisode, PayloadSummary, Unit, UnitEntity


def _now_ts_to_since_ts(now_ts: int, *, days: int) -> int:
    return int(now_ts) - max(0, int(days)) * 86400


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(payload_json: str) -> dict[str, Any]:
    try:
        data = json.loads(payload_json or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _has_pending_job(session: Session, *, kind: str, predicate) -> bool:
    rows = (
        session.query(Job.payload_json)
        .filter(
            Job.kind == kind,
            Job.status.in_([int(JobStatus.QUEUED), int(JobStatus.RUNNING)]),
        )
        .order_by(Job.id.desc())
        .limit(200)
        .all()
    )
    for (payload_json,) in rows:
        if predicate(_json_loads(payload_json)):
            return True
    return False


def _enqueue_job(session: Session, *, kind: str, payload: dict[str, Any], now_ts: int) -> None:
    session.add(
        Job(
            kind=kind,
            payload_json=_json_dumps(payload),
            status=int(JobStatus.QUEUED),
            run_after=int(now_ts),
            tries=0,
            last_error=None,
            created_at=int(now_ts),
            updated_at=int(now_ts),
        )
    )


def _latest_summary_updated_at(
    session: Session,
    *,
    scope_label: str,
    scope_key: str,
    max_sensitivity: Optional[int],
) -> Optional[int]:
    filters = [
        Unit.kind == int(UnitKind.SUMMARY),
        Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
        PayloadSummary.scope_label == str(scope_label),
        PayloadSummary.scope_key == str(scope_key),
    ]
    if max_sensitivity is not None:
        filters.append(Unit.sensitivity <= int(max_sensitivity))

    row = (
        session.query(Unit.updated_at, Unit.created_at)
        .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
        .filter(*filters)
        .order_by(Unit.updated_at.desc().nulls_last(), Unit.id.desc())
        .first()
    )
    if row is None:
        return None
    updated_at, created_at = row
    ts = int(updated_at or created_at or 0)
    return ts if ts > 0 else None


def maybe_enqueue_bond_summary(
    session: Session,
    *,
    now_ts: int,
    scope_key: str = "rolling:7d",
    cooldown_seconds: int = 6 * 3600,
    max_sensitivity: Optional[int] = int(Sensitivity.PRIVATE),
) -> bool:
    """
    bond summary ジョブを必要に応じてenqueueする。

    重複抑制とクールダウンを考慮し、新規エピソードがある場合のみ実行する。
    max_sensitivity が None の場合は sensitivity フィルタを行わない。
    """

    if _has_pending_job(
        session,
        kind="bond_summary",
        predicate=lambda p: (str(p.get("scope_key") or "").strip() in {"", scope_key}),
    ):
        return False

    updated_at = _latest_summary_updated_at(
        session,
        scope_label="bond",
        scope_key=scope_key,
        max_sensitivity=max_sensitivity,
    )
    if updated_at is None:
        # 初回起動などで「直近7日エピソードが空（または実質空）」なら、空入力でLLMを呼ばない。
        # （関係性サマリは会話が発生してから作れば十分）
        range_end = int(now_ts)
        range_start = range_end - 7 * 86400
        filters = [
            Unit.kind == int(UnitKind.EPISODE),
            Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
            Unit.occurred_at.isnot(None),
            Unit.occurred_at >= int(range_start),
            Unit.occurred_at < int(range_end),
            # worker側の行生成条件に合わせ、user/reply のどちらかが空でないものだけ対象にする。
            or_(
                func.length(func.trim(PayloadEpisode.user_text)) > 0,
                func.length(func.trim(PayloadEpisode.reply_text)) > 0,
            ),
        ]
        if max_sensitivity is not None:
            filters.append(Unit.sensitivity <= int(max_sensitivity))
        any_episode_line = (
            session.query(Unit.id)
            .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
            .filter(*filters)
            .limit(1)
            .scalar()
        )
        if any_episode_line is None:
            return False
        _enqueue_job(session, kind="bond_summary", payload={"scope_key": scope_key}, now_ts=now_ts)
        return True

    if int(now_ts) - int(updated_at) < int(cooldown_seconds):
        return False

    filters = [
        Unit.kind == int(UnitKind.EPISODE),
        Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
        Unit.occurred_at.isnot(None),
        Unit.occurred_at > int(updated_at),
    ]
    if max_sensitivity is not None:
        filters.append(Unit.sensitivity <= int(max_sensitivity))

    new_episode = session.query(Unit.id).filter(*filters).limit(1).scalar()
    if new_episode is None:
        return False

    _enqueue_job(session, kind="bond_summary", payload={"scope_key": scope_key}, now_ts=now_ts)
    return True


def maybe_enqueue_capsule_refresh(
    session: Session,
    *,
    now_ts: int,
    interval_seconds: int = 30 * 60,
    limit: int = 5,
    max_sensitivity: int = int(Sensitivity.PRIVATE),
) -> bool:
    """
    capsule_refresh ジョブを一定間隔でenqueueする。

    重複抑制を行い、前回実行から一定時間経過した場合のみ実行する。
    """
    if _has_pending_job(
        session,
        kind="capsule_refresh",
        predicate=lambda p: int(p.get("limit") or 0) == int(limit),
    ):
        return False

    last_capsule_ts = (
        session.query(Unit.updated_at, Unit.created_at)
        .filter(
            Unit.kind == int(UnitKind.CAPSULE),
            Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
            Unit.sensitivity <= int(max_sensitivity),
        )
        .order_by(Unit.updated_at.desc().nulls_last(), Unit.id.desc())
        .first()
    )
    if last_capsule_ts is not None:
        updated_at, created_at = last_capsule_ts
        ts = int(updated_at or created_at or 0)
        if ts > 0 and int(now_ts) - ts < int(interval_seconds):
            return False

    _enqueue_job(session, kind="capsule_refresh", payload={"limit": int(limit)}, now_ts=now_ts)
    return True


def _entity_has_new_episode_since(
    session: Session,
    *,
    entity_id: int,
    since_ts: int,
    max_sensitivity: int,
) -> bool:
    row = (
        session.query(Unit.id)
        .join(UnitEntity, UnitEntity.unit_id == Unit.id)
        .filter(
            UnitEntity.entity_id == int(entity_id),
            Unit.kind == int(UnitKind.EPISODE),
            Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
            Unit.sensitivity <= int(max_sensitivity),
            func.coalesce(Unit.occurred_at, Unit.created_at) > int(since_ts),
        )
        .limit(1)
        .scalar()
    )
    return row is not None


def _pick_entities_to_refresh(
    session: Session,
    *,
    now_ts: int,
    window_days: int,
    max_candidates: int,
    max_sensitivity: int,
) -> list[int]:
    """直近のエピソードで頻出した entity_id を返す（weight合計の降順）。"""
    since_ts = _now_ts_to_since_ts(now_ts, days=window_days)
    rows = (
        session.query(UnitEntity.entity_id, func.sum(UnitEntity.weight).label("w"))
        .join(Unit, Unit.id == UnitEntity.unit_id)
        .filter(
            Unit.kind == int(UnitKind.EPISODE),
            Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
            Unit.sensitivity <= int(max_sensitivity),
            func.coalesce(Unit.occurred_at, Unit.created_at) >= int(since_ts),
        )
        .group_by(UnitEntity.entity_id)
        .order_by(func.sum(UnitEntity.weight).desc(), UnitEntity.entity_id.asc())
        .limit(int(max_candidates))
        .all()
    )
    entity_ids = [int(eid) for eid, _w in rows]
    if not entity_ids:
        return []

    picked: list[int] = []
    for eid in entity_ids:
        picked.append(int(eid))
    return picked


def maybe_enqueue_entity_summaries(
    session: Session,
    *,
    now_ts: int,
    cooldown_seconds: int = 12 * 3600,
    window_days: int = 14,
    max_person: int = 3,
    max_topic: int = 3,
    max_sensitivity: int = int(Sensitivity.PRIVATE),
) -> dict[str, int]:
    """
    person/topic の summary refresh を周期的にenqueueする。

    重複抑制・クールダウン・新規Episode判定を考慮し、
    頻出エンティティの要約更新ジョブを登録する。
    """
    stats = {"person": 0, "topic": 0}
    candidate_ids = _pick_entities_to_refresh(
        session,
        now_ts=now_ts,
        window_days=window_days,
        max_candidates=max(12, max_person + max_topic),
        max_sensitivity=max_sensitivity,
    )
    if not candidate_ids:
        return stats

    person_ids: list[int] = []
    topic_ids: list[int] = []
    ent_rows = session.query(Entity.id, Entity.roles_json, Entity.normalized, Entity.name).filter(Entity.id.in_(candidate_ids)).all()
    for entity_id, roles_json, normalized, name in ent_rows:
        try:
            roles = json.loads(roles_json or "[]")
        except Exception:  # noqa: BLE001
            roles = []
        roles_s = {str(x).strip().lower() for x in roles} if isinstance(roles, list) else set()
        if "person" in roles_s and len(person_ids) < int(max_person):
            person_ids.append(int(entity_id))
        elif "topic" in roles_s and len(topic_ids) < int(max_topic):
            topic_ids.append(int(entity_id))
        if len(person_ids) >= int(max_person) and len(topic_ids) >= int(max_topic):
            break

    if person_ids:
        for entity_id in person_ids:
            if _has_pending_job(
                session,
                kind="person_summary_refresh",
                predicate=lambda p, _id=entity_id: int(p.get("entity_id") or 0) == int(_id),
            ):
                continue

            scope_key = f"person:{int(entity_id)}"
            updated_at = _latest_summary_updated_at(
                session,
                scope_label="person",
                scope_key=scope_key,
                max_sensitivity=max_sensitivity,
            )
            if updated_at is not None and int(now_ts) - int(updated_at) < int(cooldown_seconds):
                continue
            if updated_at is not None and not _entity_has_new_episode_since(
                session,
                entity_id=int(entity_id),
                since_ts=int(updated_at),
                max_sensitivity=max_sensitivity,
            ):
                continue

            _enqueue_job(session, kind="person_summary_refresh", payload={"entity_id": int(entity_id)}, now_ts=now_ts)
            stats["person"] += 1

    if topic_ids:
        # topic summary の scope_key は `topic:{topic_key}` なので、Entityから key を解決する。
        topic_key_by_id: dict[int, str] = {}
        for eid, _roles_json, normalized, name in ent_rows:
            key = str((normalized or name or "")).strip().lower()
            if key:
                topic_key_by_id[int(eid)] = key

        for entity_id in topic_ids:
            if entity_id not in topic_key_by_id:
                continue
            if _has_pending_job(
                session,
                kind="topic_summary_refresh",
                predicate=lambda p, _id=entity_id: int(p.get("entity_id") or 0) == int(_id),
            ):
                continue

            topic_key = topic_key_by_id[int(entity_id)]
            scope_key = f"topic:{topic_key}"
            updated_at = _latest_summary_updated_at(
                session,
                scope_label="topic",
                scope_key=scope_key,
                max_sensitivity=max_sensitivity,
            )
            if updated_at is not None and int(now_ts) - int(updated_at) < int(cooldown_seconds):
                continue
            if updated_at is not None and not _entity_has_new_episode_since(
                session,
                entity_id=int(entity_id),
                since_ts=int(updated_at),
                max_sensitivity=max_sensitivity,
            ):
                continue

            _enqueue_job(session, kind="topic_summary_refresh", payload={"entity_id": int(entity_id)}, now_ts=now_ts)
            stats["topic"] += 1

    return stats


@dataclass(frozen=True)
class PeriodicEnqueueConfig:
    """
    定期enqueueのチューニングパラメータ。

    クールダウン間隔、上限数、秘匿度フィルタなどを設定する。
    """
    weekly_cooldown_seconds: int = 6 * 3600
    entity_cooldown_seconds: int = 12 * 3600
    entity_window_days: int = 14
    max_person: int = 3
    max_topic: int = 3
    capsule_interval_seconds: int = 30 * 60
    capsule_limit: int = 5
    max_sensitivity: int = int(Sensitivity.PRIVATE)


def enqueue_periodic_jobs(session: Session, *, now_ts: int, config: PeriodicEnqueueConfig | None = None) -> dict[str, Any]:
    """
    定期実行のメインエントリポイント。

    必要なジョブをenqueueし、登録した件数の統計を返す。
    commitは呼び出し側で行う。
    """
    cfg = config or PeriodicEnqueueConfig()
    stats: dict[str, Any] = {
        "bond_summary": 0,
        "capsule_refresh": 0,
        "person_summary_refresh": 0,
        "topic_summary_refresh": 0,
    }

    if maybe_enqueue_bond_summary(
        session,
        now_ts=now_ts,
        cooldown_seconds=cfg.weekly_cooldown_seconds,
        max_sensitivity=cfg.max_sensitivity,
    ):
        stats["bond_summary"] += 1

    entity_stats = maybe_enqueue_entity_summaries(
        session,
        now_ts=now_ts,
        cooldown_seconds=cfg.entity_cooldown_seconds,
        window_days=cfg.entity_window_days,
        max_person=cfg.max_person,
        max_topic=cfg.max_topic,
        max_sensitivity=cfg.max_sensitivity,
    )
    stats["person_summary_refresh"] += int(entity_stats.get("person") or 0)
    stats["topic_summary_refresh"] += int(entity_stats.get("topic") or 0)

    if maybe_enqueue_capsule_refresh(
        session,
        now_ts=now_ts,
        interval_seconds=cfg.capsule_interval_seconds,
        limit=cfg.capsule_limit,
        max_sensitivity=cfg.max_sensitivity,
    ):
        stats["capsule_refresh"] += 1

    return stats
