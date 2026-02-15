"""
World Model 永続化ストア。

役割:
- 自律ループから見た永続化操作を1箇所に集約する。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from cocoro_ghost import common_utils
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import (
    WmActionResult,
    WmActionTicket,
    WmBelief,
    WmEntity,
    WmLink,
    WmObservation,
    WmStrategicGoal,
)


def _now_utc_ts() -> int:
    """現在UTC時刻をUNIX秒で返す。"""

    return int(time.time())


class WorldModelStore:
    """World Model の更新を担当する。"""

    def __init__(self, *, embedding_preset_id: str, embedding_dimension: int) -> None:
        # --- DB接続パラメータを保持 ---
        self._embedding_preset_id = str(embedding_preset_id)
        self._embedding_dimension = int(embedding_dimension)

    def upsert_entity(
        self,
        *,
        entity_key: str,
        entity_type: str,
        name: str,
        value_json: dict[str, Any],
        confidence: float,
    ) -> int:
        """エンティティを upsert して entity_id を返す。"""

        # --- 入力正規化 ---
        key = str(entity_key or "").strip()
        if not key:
            raise RuntimeError("entity_key is required")
        now_ts = _now_utc_ts()

        # --- 既存行を更新、無ければ作成 ---
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = db.query(WmEntity).filter(WmEntity.entity_key == key).one_or_none()
            if row is None:
                row = WmEntity(
                    entity_key=key,
                    entity_type=str(entity_type or "").strip() or "unknown",
                    name=str(name or "").strip() or key,
                    value_json=common_utils.json_dumps(dict(value_json or {})),
                    confidence=float(confidence),
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
                db.add(row)
                db.flush()
                return int(row.entity_id)

            row.entity_type = str(entity_type or "").strip() or row.entity_type
            row.name = str(name or "").strip() or row.name
            row.value_json = common_utils.json_dumps(dict(value_json or {}))
            row.confidence = float(confidence)
            row.updated_at = int(now_ts)
            db.add(row)
            db.flush()
            return int(row.entity_id)

    def add_observation(
        self,
        *,
        source_type: str,
        source_ref: str | None,
        content_text: str,
        payload_json: dict[str, Any],
    ) -> int:
        """観測を1件追加して observation_id を返す。"""

        # --- 観測行を追加 ---
        now_ts = _now_utc_ts()
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = WmObservation(
                source_type=str(source_type or "").strip() or "system",
                source_ref=(str(source_ref).strip() if source_ref is not None else None),
                content_text=str(content_text or "").strip(),
                payload_json=common_utils.json_dumps(dict(payload_json or {})),
                created_at=int(now_ts),
            )
            db.add(row)
            db.flush()
            return int(row.observation_id)

    def upsert_goal(
        self,
        *,
        goal_id: str,
        title: str,
        intent: str,
        priority: float,
        horizon_seconds: int,
        success_criteria: list[str],
        constraints: list[str],
        status: str,
    ) -> None:
        """戦略目標を upsert する。"""

        # --- 入力正規化 ---
        gid = str(goal_id or "").strip()
        if not gid:
            raise RuntimeError("goal_id is required")
        now_ts = _now_utc_ts()

        # --- 既存行を更新、無ければ作成 ---
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = db.query(WmStrategicGoal).filter(WmStrategicGoal.goal_id == gid).one_or_none()
            if row is None:
                row = WmStrategicGoal(
                    goal_id=gid,
                    title=str(title or "").strip() or "goal",
                    intent=str(intent or "").strip() or "observe_world",
                    priority=float(priority),
                    horizon_seconds=int(horizon_seconds),
                    success_criteria_json=common_utils.json_dumps(list(success_criteria or [])),
                    constraints_json=common_utils.json_dumps(list(constraints or [])),
                    status=str(status or "active"),
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
                db.add(row)
                return

            row.title = str(title or "").strip() or row.title
            row.intent = str(intent or "").strip() or row.intent
            row.priority = float(priority)
            row.horizon_seconds = int(horizon_seconds)
            row.success_criteria_json = common_utils.json_dumps(list(success_criteria or []))
            row.constraints_json = common_utils.json_dumps(list(constraints or []))
            row.status = str(status or row.status)
            row.updated_at = int(now_ts)
            db.add(row)

    def add_action_ticket(
        self,
        *,
        goal_id: str | None,
        capability_id: str,
        operation: str,
        input_payload: dict[str, Any],
        preconditions: list[str],
        expected_effect: list[str],
        verify: list[str],
        issued_at: int,
        deadline_at: int | None,
    ) -> str:
        """ActionTicket を追加し ticket_id を返す。"""

        # --- 行を作成 ---
        now_ts = _now_utc_ts()
        ticket_id = str(uuid.uuid4())
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = WmActionTicket(
                ticket_id=ticket_id,
                goal_id=(str(goal_id).strip() if goal_id else None),
                capability_id=str(capability_id or "").strip(),
                operation=str(operation or "").strip(),
                input_payload_json=common_utils.json_dumps(dict(input_payload or {})),
                preconditions_json=common_utils.json_dumps(list(preconditions or [])),
                expected_effect_json=common_utils.json_dumps(list(expected_effect or [])),
                verify_json=common_utils.json_dumps(list(verify or [])),
                status="queued",
                reason_code=None,
                issued_at=int(issued_at),
                deadline_at=(int(deadline_at) if deadline_at is not None else None),
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
            db.add(row)
        return ticket_id

    def update_action_ticket_status(self, *, ticket_id: str, status: str, reason_code: str | None = None) -> None:
        """ActionTicket の状態を更新する。"""

        # --- 対象行を更新 ---
        tid = str(ticket_id or "").strip()
        if not tid:
            raise RuntimeError("ticket_id is required")
        now_ts = _now_utc_ts()
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = db.query(WmActionTicket).filter(WmActionTicket.ticket_id == tid).one_or_none()
            if row is None:
                raise RuntimeError(f"action ticket not found: {tid}")
            row.status = str(status or "").strip()
            row.reason_code = (str(reason_code).strip() if reason_code is not None else None)
            row.updated_at = int(now_ts)
            db.add(row)

    def add_action_result(
        self,
        *,
        ticket_id: str | None,
        status: str,
        observations: list[dict[str, Any]],
        effects: list[dict[str, Any]],
        error_message: str | None,
        reason_code: str | None,
        finished_at: int,
    ) -> str:
        """ActionResult を追加し result_id を返す。"""

        # --- 行を作成 ---
        now_ts = _now_utc_ts()
        result_id = str(uuid.uuid4())
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = WmActionResult(
                result_id=result_id,
                ticket_id=(str(ticket_id).strip() if ticket_id else None),
                status=str(status or "").strip(),
                observations_json=common_utils.json_dumps(list(observations or [])),
                effects_json=common_utils.json_dumps(list(effects or [])),
                error_message=(str(error_message).strip() if error_message is not None else None),
                reason_code=(str(reason_code).strip() if reason_code is not None else None),
                finished_at=int(finished_at),
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
            db.add(row)
        return result_id

    def add_belief(
        self,
        *,
        subject_entity_id: int | None,
        predicate: str,
        object_text: str,
        value_json: dict[str, Any],
        confidence: float,
        source_type: str,
        evidence_event_ids: list[int],
    ) -> int:
        """belief を1件追加して belief_id を返す。"""

        # --- 行を作成 ---
        now_ts = _now_utc_ts()
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = WmBelief(
                subject_entity_id=(int(subject_entity_id) if subject_entity_id is not None else None),
                predicate=str(predicate or "").strip(),
                object_text=str(object_text or "").strip(),
                value_json=common_utils.json_dumps(dict(value_json or {})),
                confidence=float(confidence),
                source_type=str(source_type or "").strip() or "observation",
                valid_from_ts=None,
                valid_to_ts=None,
                evidence_event_ids_json=common_utils.json_dumps([int(x) for x in list(evidence_event_ids or [])]),
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
            db.add(row)
            db.flush()
            return int(row.belief_id)

    def upsert_link(
        self,
        *,
        link_type: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        confidence: float,
        evidence_event_ids: list[int],
    ) -> None:
        """World Model のリンクを upsert する。"""

        # --- 入力正規化 ---
        link_type_s = str(link_type or "").strip()
        from_type_s = str(from_type or "").strip()
        from_id_s = str(from_id or "").strip()
        to_type_s = str(to_type or "").strip()
        to_id_s = str(to_id or "").strip()
        if not (link_type_s and from_type_s and from_id_s and to_type_s and to_id_s):
            raise RuntimeError("link route fields are required")
        now_ts = _now_utc_ts()

        # --- 既存更新 / 新規作成 ---
        with memory_session_scope(self._embedding_preset_id, self._embedding_dimension) as db:
            row = (
                db.query(WmLink)
                .filter(WmLink.link_type == link_type_s)
                .filter(WmLink.from_type == from_type_s)
                .filter(WmLink.from_id == from_id_s)
                .filter(WmLink.to_type == to_type_s)
                .filter(WmLink.to_id == to_id_s)
                .one_or_none()
            )
            if row is None:
                row = WmLink(
                    link_type=link_type_s,
                    from_type=from_type_s,
                    from_id=from_id_s,
                    to_type=to_type_s,
                    to_id=to_id_s,
                    confidence=float(confidence),
                    evidence_event_ids_json=common_utils.json_dumps([int(x) for x in list(evidence_event_ids or [])]),
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
                db.add(row)
                return

            row.confidence = float(confidence)
            row.evidence_event_ids_json = common_utils.json_dumps([int(x) for x in list(evidence_event_ids or [])])
            row.updated_at = int(now_ts)
            db.add(row)
