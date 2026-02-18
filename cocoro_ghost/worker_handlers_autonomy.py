"""
Workerジョブハンドラ（autonomy）。

役割:
    - deliberate_once: Trigger から ActionDecision/Intent を生成する。
    - execute_intent: Capability を実行して ActionResult を保存する。
    - promote_action_result_to_searchable: recall可否を最終確定する。
    - snapshot_runtime: runtime snapshot を保存する。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost.autonomy.capabilities.web_access import execute_web_research
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult, parse_action_decision
from cocoro_ghost.clock import get_clock_service
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import (
    ActionDecision,
    ActionResult,
    AutonomyTrigger,
    Event,
    EventAffect,
    Intent,
    Job,
    RuntimeSnapshot,
    State,
)
from cocoro_ghost.worker_constants import JOB_PENDING as _JOB_PENDING
from cocoro_ghost.worker_handlers_common import _now_utc_ts


logger = logging.getLogger(__name__)


def _now_domain_utc_ts() -> int:
    """domain時刻（UTC UNIX秒）を返す。"""
    return int(get_clock_service().now_domain_utc_ts())


def _load_recent_mood_snapshot(db) -> dict[str, Any]:
    """Deliberation入力用の Mood スナップショットを返す。"""

    # --- long_mood_state（最新1件） ---
    long_mood = (
        db.query(State)
        .filter(State.searchable == 1)
        .filter(State.kind == "long_mood_state")
        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
        .first()
    )
    long_mood_payload = None
    if long_mood is not None:
        long_mood_payload = {
            "state_id": int(long_mood.state_id),
            "body_text": str(long_mood.body_text),
            "payload_json": str(long_mood.payload_json),
            "last_confirmed_at": int(long_mood.last_confirmed_at),
        }

    # --- 直近 affect のVAD平均 ---
    affects = db.query(EventAffect).order_by(EventAffect.created_at.desc()).limit(12).all()
    if not affects:
        return {
            "long_mood_state": long_mood_payload,
            "recent_vad_average": {"v": 0.0, "a": 0.0, "d": 0.0},
        }
    v = sum(float(a.vad_v) for a in affects) / float(len(affects))
    a = sum(float(x.vad_a) for x in affects) / float(len(affects))
    d = sum(float(x.vad_d) for x in affects) / float(len(affects))
    return {
        "long_mood_state": long_mood_payload,
        "recent_vad_average": {"v": float(v), "a": float(a), "d": float(d)},
    }


def _collect_deliberation_input(db, *, trigger: AutonomyTrigger) -> dict[str, Any]:
    """DeliberationContextPack 相当の入力を構築する。"""

    # --- events（会話想起ノイズ制御: deliberation_decision は常時除外） ---
    recent_events = (
        db.query(Event)
        .filter(Event.searchable == 1)
        .filter(Event.source != "deliberation_decision")
        .order_by(Event.event_id.desc())
        .limit(24)
        .all()
    )
    event_rows = [
        {
            "event_id": int(x.event_id),
            "created_at": int(x.created_at),
            "source": str(x.source),
            "user_text": str(x.user_text or "")[:800],
            "assistant_text": str(x.assistant_text or "")[:800],
        }
        for x in recent_events
    ]

    # --- state ---
    recent_states = (
        db.query(State)
        .filter(State.searchable == 1)
        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
        .limit(24)
        .all()
    )
    state_rows = [
        {
            "state_id": int(s.state_id),
            "kind": str(s.kind),
            "body_text": str(s.body_text)[:800],
            "payload_json": str(s.payload_json)[:800],
            "confidence": float(s.confidence),
            "last_confirmed_at": int(s.last_confirmed_at),
        }
        for s in recent_states
    ]

    # --- goals ---
    goals_rows = db.execute(
        text(
            """
            SELECT goal_id, title, goal_type, status, priority, target_condition_json, horizon
              FROM goals
             WHERE status = 'active'
             ORDER BY priority DESC, updated_at DESC
             LIMIT 8
            """
        )
    ).fetchall()
    goals = [
        {
            "goal_id": str(r[0]),
            "title": str(r[1]),
            "goal_type": str(r[2]),
            "status": str(r[3]),
            "priority": int(r[4] or 0),
            "target_condition_json": str(r[5] or "{}"),
            "horizon": str(r[6] or ""),
        }
        for r in goals_rows
    ]

    # --- intents ---
    intent_rows = (
        db.query(Intent)
        .filter(Intent.status.in_(["queued", "running", "blocked"]))
        .order_by(Intent.updated_at.desc(), Intent.created_at.desc())
        .limit(8)
        .all()
    )
    intents = [
        {
            "intent_id": str(x.intent_id),
            "decision_id": str(x.decision_id),
            "action_type": str(x.action_type),
            "status": str(x.status),
            "priority": int(x.priority),
            "scheduled_at": (int(x.scheduled_at) if x.scheduled_at is not None else None),
            "blocked_reason": (str(x.blocked_reason) if x.blocked_reason is not None else None),
        }
        for x in intent_rows
    ]

    # --- trigger payload ---
    trigger_payload = common_utils.json_loads_maybe(str(trigger.payload_json or ""))
    if not isinstance(trigger_payload, dict):
        trigger_payload = {}

    return {
        "trigger": {
            "trigger_id": str(trigger.trigger_id),
            "trigger_type": str(trigger.trigger_type),
            "trigger_key": str(trigger.trigger_key),
            "source_event_id": (int(trigger.source_event_id) if trigger.source_event_id is not None else None),
            "payload": trigger_payload,
            "scheduled_at": (int(trigger.scheduled_at) if trigger.scheduled_at is not None else None),
        },
        "events": event_rows,
        "states": state_rows,
        "goals": goals,
        "intents": intents,
        "mood": _load_recent_mood_snapshot(db),
        "capabilities": [
            {
                "capability": "web_access",
                "action_types": ["web_research"],
            }
        ],
    }


def _mark_trigger_done(
    *,
    db,
    trigger_id: str,
    claim_token: str,
    now_system_ts: int,
) -> None:
    """claimed trigger を done へ遷移する。"""
    db.execute(
        text(
            """
            UPDATE autonomy_triggers
               SET status='done',
                   claim_token=NULL,
                   claimed_at=NULL,
                   updated_at=:updated_at
             WHERE trigger_id=:trigger_id
               AND status='claimed'
               AND claim_token=:claim_token
            """
        ),
        {
            "updated_at": int(now_system_ts),
            "trigger_id": str(trigger_id),
            "claim_token": str(claim_token),
        },
    )


def _mark_trigger_dropped(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    trigger_id: str,
    claim_token: str,
    now_system_ts: int,
    reason: str,
) -> None:
    """claimed trigger を dropped へ遷移する。"""
    dropped_reason = str(reason or "").strip()
    if not dropped_reason:
        dropped_reason = "unknown_error"
    if len(dropped_reason) > 1000:
        dropped_reason = dropped_reason[:1000]
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.execute(
            text(
                """
                UPDATE autonomy_triggers
                   SET status='dropped',
                       dropped_reason=:dropped_reason,
                       dropped_at=:dropped_at,
                       last_error=:last_error,
                       updated_at=:updated_at
                 WHERE trigger_id=:trigger_id
                   AND status='claimed'
                   AND claim_token=:claim_token
                """
            ),
            {
                "dropped_reason": str(dropped_reason),
                "dropped_at": int(now_system_ts),
                "last_error": str(dropped_reason),
                "updated_at": int(now_system_ts),
                "trigger_id": str(trigger_id),
                "claim_token": str(claim_token),
            },
        )


def _handle_deliberate_once(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """Trigger から ActionDecision を作成し、必要なら Intent を生成する。"""

    # --- payload を検証 ---
    trigger_id = str(payload.get("trigger_id") or "").strip()
    claim_token = str(payload.get("claim_token") or "").strip()
    if not trigger_id or not claim_token:
        raise RuntimeError("payload.trigger_id and payload.claim_token are required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()

    try:
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- claimed trigger を取得 ---
            trigger = (
                db.query(AutonomyTrigger)
                .filter(AutonomyTrigger.trigger_id == str(trigger_id))
                .filter(AutonomyTrigger.status == "claimed")
                .filter(AutonomyTrigger.claim_token == str(claim_token))
                .one_or_none()
            )
            if trigger is None:
                return

            # --- defer_until前の早着トリガは再スケジュール ---
            if trigger.scheduled_at is not None and int(trigger.scheduled_at) > int(now_domain_ts):
                db.execute(
                    text(
                        """
                        UPDATE autonomy_triggers
                           SET status='queued',
                               claim_token=NULL,
                               claimed_at=NULL,
                               updated_at=:updated_at
                         WHERE trigger_id=:trigger_id
                           AND status='claimed'
                           AND claim_token=:claim_token
                        """
                    ),
                    {
                        "updated_at": int(now_system_ts),
                        "trigger_id": str(trigger_id),
                        "claim_token": str(claim_token),
                    },
                )
                return

            # --- Deliberation入力 ---
            deliberation_input = _collect_deliberation_input(db, trigger=trigger)
            cfg = get_config_store().config
            system_prompt = prompt_builders.autonomy_deliberation_system_prompt(
                persona_text=str(cfg.persona_text),
                addon_text=str(cfg.addon_text),
                second_person_label=str(cfg.second_person_label),
            )

            # --- LLMで意思決定 ---
            resp = llm_client.generate_json_response(
                system_prompt=system_prompt,
                input_text=common_utils.json_dumps(deliberation_input),
                purpose=LlmRequestPurpose.ASYNC_AUTONOMY_DELIBERATION,
                max_tokens=int(cfg.deliberation_max_tokens),
                model_override=str(cfg.deliberation_model),
            )
            decision_raw = llm_client.response_json(resp)
            if not isinstance(decision_raw, dict):
                raise ValueError("deliberation output is not a JSON object")
            decision = parse_action_decision(decision_raw)

            # --- event: deliberation_decision（常時 searchable=0） ---
            decision_event = Event(
                created_at=int(now_domain_ts),
                updated_at=int(now_domain_ts),
                searchable=0,
                client_id=None,
                source="deliberation_decision",
                user_text=str(decision.reason_text),
                assistant_text=None,
                entities_json="[]",
                client_context_json=common_utils.json_dumps(
                    {
                        "trigger_id": str(trigger_id),
                        "trigger_type": str(trigger.trigger_type),
                        "trigger_key": str(trigger.trigger_key),
                        "decision": decision_raw,
                    }
                ),
            )
            db.add(decision_event)
            db.flush()

            # --- ActionDecision を保存 ---
            decision_id = str(uuid.uuid4())
            db.add(
                ActionDecision(
                    decision_id=str(decision_id),
                    event_id=int(decision_event.event_id),
                    trigger_type=str(trigger.trigger_type),
                    trigger_ref=str(trigger.trigger_id),
                    do_action=int(decision.do_action),
                    decision_outcome=str(decision.decision_outcome),
                    action_type=(str(decision.action_type) if decision.action_type else None),
                    action_payload_json=(str(decision.action_payload_json) if decision.action_payload_json else None),
                    reason_text=str(decision.reason_text),
                    defer_reason=(str(decision.defer_reason) if decision.defer_reason else None),
                    defer_until=(int(decision.defer_until) if decision.defer_until is not None else None),
                    next_deliberation_at=(
                        int(decision.next_deliberation_at) if decision.next_deliberation_at is not None else None
                    ),
                    persona_influence_json=str(decision.persona_influence_json),
                    mood_influence_json=str(decision.mood_influence_json),
                    evidence_event_ids_json=str(decision.evidence_event_ids_json),
                    evidence_state_ids_json=str(decision.evidence_state_ids_json),
                    evidence_goal_ids_json=str(decision.evidence_goal_ids_json),
                    confidence=float(decision.confidence),
                    created_at=int(now_system_ts),
                )
            )

            # --- do_action: Intent を冪等生成して execute_intent を投入 ---
            if str(decision.decision_outcome) == "do_action":
                proposed_intent_id = str(uuid.uuid4())
                db.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO intents(
                            intent_id,
                            decision_id,
                            goal_id,
                            action_type,
                            action_payload_json,
                            status,
                            priority,
                            scheduled_at,
                            blocked_reason,
                            dropped_reason,
                            dropped_at,
                            last_result_status,
                            created_at,
                            updated_at
                        )
                        VALUES(
                            :intent_id,
                            :decision_id,
                            NULL,
                            :action_type,
                            :action_payload_json,
                            'queued',
                            :priority,
                            NULL,
                            NULL,
                            '',
                            NULL,
                            NULL,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    {
                        "intent_id": str(proposed_intent_id),
                        "decision_id": str(decision_id),
                        "action_type": str(decision.action_type or ""),
                        "action_payload_json": str(decision.action_payload_json or "{}"),
                        "priority": int(decision.priority),
                        "created_at": int(now_system_ts),
                        "updated_at": int(now_system_ts),
                    },
                )
                intent_row = db.execute(
                    text("SELECT intent_id FROM intents WHERE decision_id=:decision_id LIMIT 1"),
                    {"decision_id": str(decision_id)},
                ).fetchone()
                if intent_row is not None and str(intent_row[0] or "").strip():
                    db.add(
                        Job(
                            kind="execute_intent",
                            payload_json=common_utils.json_dumps({"intent_id": str(intent_row[0])}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_system_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_system_ts),
                            updated_at=int(now_system_ts),
                        )
                    )

            # --- defer: 再検討トリガを投入 ---
            if str(decision.decision_outcome) == "defer":
                defer_at = int(decision.next_deliberation_at or now_domain_ts)
                db.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO autonomy_triggers(
                            trigger_id,
                            trigger_type,
                            trigger_key,
                            source_event_id,
                            payload_json,
                            status,
                            scheduled_at,
                            claim_token,
                            claimed_at,
                            attempts,
                            last_error,
                            dropped_reason,
                            dropped_at,
                            created_at,
                            updated_at
                        )
                        VALUES(
                            :trigger_id,
                            'heartbeat',
                            :trigger_key,
                            NULL,
                            :payload_json,
                            'queued',
                            :scheduled_at,
                            NULL,
                            NULL,
                            0,
                            NULL,
                            '',
                            NULL,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    {
                        "trigger_id": str(uuid.uuid4()),
                        "trigger_key": f"defer:{str(decision_id)}:{int(defer_at)}",
                        "payload_json": common_utils.json_dumps(
                            {
                                "source": "defer",
                                "decision_id": str(decision_id),
                                "defer_reason": str(decision.defer_reason or ""),
                            }
                        ),
                        "scheduled_at": int(defer_at),
                        "created_at": int(now_system_ts),
                        "updated_at": int(now_system_ts),
                    },
                )

            # --- trigger を done へ遷移 ---
            _mark_trigger_done(
                db=db,
                trigger_id=str(trigger_id),
                claim_token=str(claim_token),
                now_system_ts=int(now_system_ts),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("deliberate_once failed trigger_id=%s", str(trigger_id))
        _mark_trigger_dropped(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            trigger_id=str(trigger_id),
            claim_token=str(claim_token),
            now_system_ts=int(now_system_ts),
            reason=str(exc),
        )


def _handle_execute_intent(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """Intent を実行して ActionResult を保存する。"""

    # --- payload を検証 ---
    intent_id = str(payload.get("intent_id") or "").strip()
    if not intent_id:
        raise RuntimeError("payload.intent_id is required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    cfg = get_config_store().config

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- queued -> running を原子的に claim ---
        claimed = db.execute(
            text(
                """
                UPDATE intents
                   SET status='running',
                       updated_at=:updated_at
                 WHERE intent_id=:intent_id
                   AND status='queued'
                """
            ),
            {"updated_at": int(now_system_ts), "intent_id": str(intent_id)},
        )
        if int(claimed.rowcount or 0) != 1:
            return

        # --- claim後の intent/decision を読む ---
        intent = db.query(Intent).filter(Intent.intent_id == str(intent_id)).one_or_none()
        if intent is None:
            return
        decision = db.query(ActionDecision).filter(ActionDecision.decision_id == str(intent.decision_id)).one_or_none()
        if decision is None:
            # decision が無ければ実行不能として dropped
            db.execute(
                text(
                    """
                    UPDATE intents
                       SET status='dropped',
                           dropped_reason=:dropped_reason,
                           dropped_at=:dropped_at,
                           updated_at=:updated_at
                     WHERE intent_id=:intent_id
                    """
                ),
                {
                    "dropped_reason": "decision_not_found",
                    "dropped_at": int(now_domain_ts),
                    "updated_at": int(now_system_ts),
                    "intent_id": str(intent_id),
                },
            )
            return

        # --- payload を読み取る ---
        intent_payload = common_utils.json_loads_maybe(str(intent.action_payload_json or ""))
        if not isinstance(intent_payload, dict):
            intent_payload = {}

        # --- capability 実行 ---
        if str(intent.action_type) == "web_research":
            result = execute_web_research(
                llm_client=llm_client,
                second_person_label=str(cfg.second_person_label),
                query=str(intent_payload.get("query") or ""),
                goal=str(intent_payload.get("goal") or ""),
                constraints=[str(x) for x in list(intent_payload.get("constraints") or [])],
            )
            capability_name = "web_access"
        else:
            capability_name = "unknown"
            result = CapabilityExecutionResult(
                result_status="failed",
                summary=f"unsupported action_type: {str(intent.action_type)}",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": str(intent.action_type),
                        "action_payload": intent_payload,
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )

        # --- event: action_result（既定 searchable=0） ---
        result_event = Event(
            created_at=int(now_domain_ts),
            updated_at=int(now_domain_ts),
            searchable=0,
            client_id=None,
            source="action_result",
            user_text=str(result.summary),
            assistant_text=None,
            entities_json="[]",
            client_context_json=common_utils.json_dumps(
                {
                    "intent_id": str(intent.intent_id),
                    "decision_id": str(decision.decision_id),
                    "action_type": str(intent.action_type),
                    "capability": str(capability_name),
                    "result_status": str(result.result_status),
                }
            ),
        )
        db.add(result_event)
        db.flush()

        # --- action_results に保存 ---
        result_id = str(uuid.uuid4())
        db.add(
            ActionResult(
                result_id=str(result_id),
                event_id=int(result_event.event_id),
                intent_id=str(intent.intent_id),
                decision_id=str(decision.decision_id),
                capability_name=str(capability_name),
                result_status=str(result.result_status),
                result_payload_json=str(result.result_payload_json),
                summary_text=str(result.summary),
                useful_for_recall_hint=int(result.useful_for_recall_hint),
                recall_decision=-1,
                recall_decided_at=None,
                created_at=int(now_system_ts),
            )
        )

        # --- intent 状態を終端へ遷移 ---
        if str(result.result_status) == "failed":
            db.execute(
                text(
                    """
                    UPDATE intents
                       SET status='dropped',
                           dropped_reason=:dropped_reason,
                           dropped_at=:dropped_at,
                           last_result_status=:last_result_status,
                           updated_at=:updated_at
                     WHERE intent_id=:intent_id
                    """
                ),
                {
                    "dropped_reason": str(result.summary)[:1000],
                    "dropped_at": int(now_domain_ts),
                    "last_result_status": str(result.result_status),
                    "updated_at": int(now_system_ts),
                    "intent_id": str(intent_id),
                },
            )
        else:
            db.execute(
                text(
                    """
                    UPDATE intents
                       SET status='done',
                           last_result_status=:last_result_status,
                           updated_at=:updated_at
                     WHERE intent_id=:intent_id
                    """
                ),
                {
                    "last_result_status": str(result.result_status),
                    "updated_at": int(now_system_ts),
                    "intent_id": str(intent_id),
                },
            )

        # --- next_trigger があれば投入 ---
        if isinstance(result.next_trigger, dict) and bool(result.next_trigger.get("required")):
            trigger_type = str(result.next_trigger.get("trigger_type") or "event")
            trigger_key = str(result.next_trigger.get("trigger_key") or f"next:{str(result_id)}")
            trigger_payload = (
                result.next_trigger.get("payload") if isinstance(result.next_trigger.get("payload"), dict) else {}
            )
            trigger_scheduled_at_raw = result.next_trigger.get("scheduled_at")
            trigger_scheduled_at = (
                int(trigger_scheduled_at_raw)
                if trigger_scheduled_at_raw is not None
                else int(now_domain_ts)
            )
            db.execute(
                text(
                    """
                    INSERT OR IGNORE INTO autonomy_triggers(
                        trigger_id,
                        trigger_type,
                        trigger_key,
                        source_event_id,
                        payload_json,
                        status,
                        scheduled_at,
                        claim_token,
                        claimed_at,
                        attempts,
                        last_error,
                        dropped_reason,
                        dropped_at,
                        created_at,
                        updated_at
                    )
                    VALUES(
                        :trigger_id,
                        :trigger_type,
                        :trigger_key,
                        :source_event_id,
                        :payload_json,
                        'queued',
                        :scheduled_at,
                        NULL,
                        NULL,
                        0,
                        NULL,
                        '',
                        NULL,
                        :created_at,
                        :updated_at
                    )
                    """
                ),
                {
                    "trigger_id": str(uuid.uuid4()),
                    "trigger_type": str(trigger_type),
                    "trigger_key": str(trigger_key),
                    "source_event_id": int(result_event.event_id),
                    "payload_json": common_utils.json_dumps(trigger_payload),
                    "scheduled_at": int(trigger_scheduled_at),
                    "created_at": int(now_system_ts),
                    "updated_at": int(now_system_ts),
                },
            )

        # --- recall判定ジョブを投入 ---
        db.add(
            Job(
                kind="promote_action_result_to_searchable",
                payload_json=common_utils.json_dumps({"result_id": str(result_id)}),
                status=int(_JOB_PENDING),
                run_after=int(now_system_ts),
                tries=0,
                last_error=None,
                created_at=int(now_system_ts),
                updated_at=int(now_system_ts),
            )
        )

        # --- 記憶更新（WritePlan）ジョブを投入 ---
        db.add(
            Job(
                kind="generate_write_plan",
                payload_json=common_utils.json_dumps({"event_id": int(result_event.event_id)}),
                status=int(_JOB_PENDING),
                run_after=int(now_system_ts),
                tries=0,
                last_error=None,
                created_at=int(now_system_ts),
                updated_at=int(now_system_ts),
            )
        )


def _handle_promote_action_result_to_searchable(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    payload: dict[str, Any],
) -> None:
    """ActionResult の recall 可否を最終確定する。"""

    # --- payload を検証 ---
    result_id = str(payload.get("result_id") or "").strip()
    if not result_id:
        raise RuntimeError("payload.result_id is required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        result = db.query(ActionResult).filter(ActionResult.result_id == str(result_id)).one_or_none()
        if result is None:
            return
        if int(result.recall_decision) != -1:
            return

        # --- 最終判定（唯一責務） ---
        summary = str(result.summary_text or "").strip()
        should_promote = (
            int(result.useful_for_recall_hint) == 1
            and str(result.result_status) in {"success", "partial"}
            and bool(summary)
        )
        decision = 1 if bool(should_promote) else 0
        result.recall_decision = int(decision)
        result.recall_decided_at = int(now_system_ts)

        # --- 採用時だけ events.searchable=1 へ昇格 ---
        if int(decision) == 1:
            db.execute(
                text(
                    """
                    UPDATE events
                       SET searchable=1,
                           updated_at=:updated_at
                     WHERE event_id=:event_id
                    """
                ),
                {
                    "updated_at": int(now_domain_ts),
                    "event_id": int(result.event_id),
                },
            )


def _handle_snapshot_runtime(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    payload: dict[str, Any],
) -> None:
    """Runtime Blackboard のスナップショットを保存する。"""

    now_system_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 稼働中 intent を収集 ---
        active_intents = (
            db.query(Intent.intent_id)
            .filter(Intent.status.in_(["queued", "running", "blocked"]))
            .order_by(Intent.updated_at.desc())
            .limit(256)
            .all()
        )
        active_intent_ids = [str(r[0]) for r in active_intents if r and str(r[0] or "").strip()]

        # --- trigger/intents の滞留数 ---
        trigger_counts = db.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) AS claimed
                  FROM autonomy_triggers
                """
            )
        ).fetchone()
        intent_counts = db.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked
                  FROM intents
                """
            )
        ).fetchone()

        snapshot_kind = str(payload.get("snapshot_kind") or "periodic")
        snapshot_payload = {
            "active_intent_ids": active_intent_ids,
            "trigger_counts": {
                "queued": int((trigger_counts[0] if trigger_counts and trigger_counts[0] is not None else 0) or 0),
                "claimed": int((trigger_counts[1] if trigger_counts and trigger_counts[1] is not None else 0) or 0),
            },
            "intent_counts": {
                "queued": int((intent_counts[0] if intent_counts and intent_counts[0] is not None else 0) or 0),
                "running": int((intent_counts[1] if intent_counts and intent_counts[1] is not None else 0) or 0),
                "blocked": int((intent_counts[2] if intent_counts and intent_counts[2] is not None else 0) or 0),
            },
            "worker_now_system_utc_ts": int(now_system_ts),
        }

        db.add(
            RuntimeSnapshot(
                snapshot_kind=str(snapshot_kind),
                payload_json=common_utils.json_dumps(snapshot_payload),
                created_at=int(now_system_ts),
            )
        )
