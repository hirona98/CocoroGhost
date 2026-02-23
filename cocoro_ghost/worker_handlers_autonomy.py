"""
Workerジョブハンドラ（autonomy）。

役割:
    - deliberate_once: Trigger から ActionDecision/Intent を生成する。
    - execute_intent: Capability を実行して ActionResult を保存する。
    - promote_action_result_to_searchable: recall可否を最終確定する。
    - snapshot_runtime: runtime snapshot を保存する。
    - sweep_agent_jobs: stale な agent_jobs を timeout 終端する。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost.autonomy.capabilities.device_control import execute_device_control
from cocoro_ghost.autonomy.capabilities.mobility_move import execute_mobility_move
from cocoro_ghost.autonomy.capabilities.schedule_alarm import execute_schedule_alarm
from cocoro_ghost.autonomy.capabilities.vision_perception import execute_vision_perception
from cocoro_ghost.autonomy.capabilities.web_access import execute_web_research
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult, parse_action_decision
from cocoro_ghost.autonomy.runtime_blackboard import get_runtime_blackboard
from cocoro_ghost.clock import get_clock_service
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import (
    ActionDecision,
    ActionResult,
    AgentJob,
    AutonomyTrigger,
    Event,
    EventAffect,
    Intent,
    Job,
    Goal,
    RuntimeSnapshot,
    State,
)
from cocoro_ghost.worker_constants import AGENT_JOB_STALE_SECONDS as _AGENT_JOB_STALE_SECONDS
from cocoro_ghost.worker_constants import JOB_PENDING as _JOB_PENDING
from cocoro_ghost.worker_constants import JOB_RUNNING as _JOB_RUNNING
from cocoro_ghost.worker_handlers_common import _now_utc_ts


logger = logging.getLogger(__name__)


class _DeliberationInvalidOutputError(RuntimeError):
    """Deliberation の LLM 出力不正（JSON/契約違反）を表す内部例外。"""

    # --- 種別コードで trigger dropped の理由を安定化する ---
    def __init__(self, *, drop_reason: str, detail: str) -> None:
        super().__init__(str(detail))
        self.drop_reason = str(drop_reason)
        self.detail = str(detail)


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
        mood_payload_obj = common_utils.json_loads_maybe(str(long_mood.payload_json or ""))
        long_mood_payload = {
            "state_id": int(long_mood.state_id),
            "body_text": str(long_mood.body_text),
            "payload": mood_payload_obj,
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


def _maybe_create_goal_from_decision(
    *,
    db,
    decision_id: str,
    action_type: str,
    action_payload_json: str | None,
    priority: int,
    now_system_ts: int,
) -> str | None:
    """ActionDecision から goals を1件生成し、goal_id を返す。"""

    # --- action_payload を読む ---
    payload = common_utils.json_loads_maybe(str(action_payload_json or ""))
    if not isinstance(payload, dict):
        payload = {}

    # --- goal 文言が無ければ作らない ---
    goal_title = str(payload.get("goal") or "").strip()
    if not goal_title:
        return None

    # --- goal_type を action_type から決める ---
    action_type_norm = str(action_type or "").strip()
    goal_type = "research" if action_type_norm == "web_research" else "task"

    # --- Goal を1件作成（単一ユーザー前提・decision単位） ---
    goal_id = str(uuid.uuid4())
    db.add(
        Goal(
            goal_id=str(goal_id),
            title=str(goal_title)[:240],
            goal_type=str(goal_type),
            status="active",
            priority=max(0, min(100, int(priority))),
            target_condition_json=common_utils.json_dumps(
                {
                    "decision_id": str(decision_id),
                    "action_type": str(action_type_norm),
                    "action_payload": payload,
                }
            ),
            horizon="short",
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )
    return str(goal_id)


def _upsert_world_model_item_from_action_result(
    *,
    db,
    source_event_id: int,
    source_result_id: str,
    capability_name: str,
    result_status: str,
    summary_text: str,
    result_payload_json: str,
    now_system_ts: int,
    now_domain_ts: int,
) -> None:
    """ActionResult を world_model_items に集約する。"""

    # --- web_access 以外は現時点では対象外 ---
    if str(capability_name or "").strip() != "web_access":
        return

    # --- payload を読む ---
    payload = common_utils.json_loads_maybe(str(result_payload_json or ""))
    if not isinstance(payload, dict):
        payload = {}

    query = str(payload.get("query") or "").strip()
    goal = str(payload.get("goal") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    findings_raw = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    sources_raw = payload.get("sources") if isinstance(payload.get("sources"), list) else []

    # --- findings を短い文字列配列へ正規化 ---
    findings: list[str] = []
    for x in findings_raw:
        if isinstance(x, dict):
            text_v = str(x.get("text") or x.get("summary") or x.get("title") or "").strip()
        else:
            text_v = str(x or "").strip()
        if not text_v:
            continue
        findings.append(text_v[:500])
        if len(findings) >= 20:
            break

    # --- sources を監査しやすい形に正規化 ---
    sources: list[dict[str, Any]] = []
    for x in sources_raw:
        if isinstance(x, dict):
            url = str(x.get("url") or "").strip()
            title = str(x.get("title") or "").strip()
        else:
            url = str(x or "").strip()
            title = ""
        if not url and not title:
            continue
        sources.append({"url": url[:1000], "title": title[:240]})
        if len(sources) >= 10:
            break

    # --- observation_class / confidence を決める ---
    result_status_norm = str(result_status or "").strip()
    has_findings = bool(findings)
    has_sources = bool(sources)
    if result_status_norm == "success" and has_findings and has_sources:
        observation_class = "fact"
        confidence = 0.93
    elif result_status_norm in {"success", "partial"} and has_findings:
        observation_class = "fact"
        confidence = 0.84 if result_status_norm == "partial" else 0.88
    elif result_status_norm in {"success", "partial"} and has_sources:
        observation_class = "inferred"
        confidence = 0.70
    elif result_status_norm == "no_effect":
        observation_class = "unknown"
        confidence = 0.20
    else:
        observation_class = "unknown"
        confidence = 0.10

    # --- item_key（同一 query の観測を統合） ---
    query_key = str(query).strip().lower()
    if not query_key:
        query_key = hashlib.sha256(str(summary_text or "").encode("utf-8")).hexdigest()[:24]
    item_key = f"web_research:{query_key}"

    # --- 保存JSON ---
    entity_obj = {
        "kind": "web_research_query",
        "query": str(query),
        "goal": str(goal),
    }
    relation_obj = {
        "result_status": str(result_status_norm),
        "capability_name": "web_access",
    }
    location_obj = {
        "space": "web",
    }
    affordance_obj = {
        "summary": str(summary_text or "")[:1000],
        "findings": list(findings),
        "sources": list(sources),
        "notes": str(notes)[:1000],
    }

    # --- フィンガープリント（内容比較用） ---
    fingerprint_payload = {
        "summary": str(summary_text or ""),
        "findings": list(findings),
        "sources": list(sources),
        "result_status": str(result_status_norm),
    }
    content_fingerprint = hashlib.sha256(
        common_utils.json_dumps(fingerprint_payload).encode("utf-8")
    ).hexdigest()

    # --- world_model_items を item_key 単位で原子的 UPSERT ---
    db.execute(
        text(
            """
            INSERT INTO world_model_items(
                item_id,
                source_event_id,
                source_result_id,
                item_key,
                observation_class,
                entity_json,
                relation_json,
                location_json,
                affordance_json,
                content_fingerprint,
                observation_count,
                confidence,
                freshness_at,
                active,
                created_at,
                updated_at
            )
            VALUES(
                :item_id,
                :source_event_id,
                :source_result_id,
                :item_key,
                :observation_class,
                :entity_json,
                :relation_json,
                :location_json,
                :affordance_json,
                :content_fingerprint,
                1,
                :confidence,
                :freshness_at,
                1,
                :created_at,
                :updated_at
            )
            ON CONFLICT(item_key) DO UPDATE SET
                source_event_id = excluded.source_event_id,
                source_result_id = excluded.source_result_id,
                observation_class = excluded.observation_class,
                entity_json = excluded.entity_json,
                relation_json = excluded.relation_json,
                location_json = excluded.location_json,
                affordance_json = excluded.affordance_json,
                content_fingerprint = excluded.content_fingerprint,
                observation_count = world_model_items.observation_count + 1,
                confidence = CASE
                    WHEN world_model_items.content_fingerprint = excluded.content_fingerprint THEN
                        MIN(1.0, world_model_items.confidence * 0.80 + excluded.confidence * 0.20 + 0.05)
                    ELSE
                        MAX(0.0, world_model_items.confidence * 0.60 + excluded.confidence * 0.40 - 0.15)
                END,
                freshness_at = MAX(COALESCE(world_model_items.freshness_at, 0), excluded.freshness_at),
                active = excluded.active,
                updated_at = excluded.updated_at
            """
        ),
        {
            "item_id": str(uuid.uuid4()),
            "source_event_id": int(source_event_id),
            "source_result_id": str(source_result_id),
            "item_key": str(item_key),
            "observation_class": str(observation_class),
            "entity_json": common_utils.json_dumps(entity_obj),
            "relation_json": common_utils.json_dumps(relation_obj),
            "location_json": common_utils.json_dumps(location_obj),
            "affordance_json": common_utils.json_dumps(affordance_obj),
            "content_fingerprint": str(content_fingerprint),
            "confidence": float(max(0.0, min(1.0, confidence))),
            "freshness_at": int(max(0, int(now_domain_ts))),
            "created_at": int(now_system_ts),
            "updated_at": int(now_system_ts),
        },
    )


def _enqueue_standard_post_action_jobs(
    *,
    db,
    event_id: int,
    result_id: str,
    now_system_ts: int,
) -> None:
    """ActionResult 保存後に共通で投入する worker jobs を追加する。"""

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
            payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
            status=int(_JOB_PENDING),
            run_after=int(now_system_ts),
            tries=0,
            last_error=None,
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )


def _finalize_intent_and_save_action_result(
    *,
    db,
    intent: Intent,
    decision_id: str,
    action_type: str,
    capability_name: str,
    result_status: str,
    summary_text: str,
    result_payload_json: str,
    useful_for_recall_hint: int,
    now_system_ts: int,
    now_domain_ts: int,
    goal_id_hint: str | None = None,
    next_trigger: dict[str, Any] | None = None,
) -> str:
    """
    ActionResult/Event を保存し、Intent を終端化する（execute_intent/callback 共通処理）。

    Returns:
        保存した ActionResult.result_id
    """

    # --- event: action_result（既定 searchable=0） ---
    result_event = Event(
        created_at=int(now_domain_ts),
        updated_at=int(now_domain_ts),
        searchable=0,
        client_id=None,
        source="action_result",
        user_text=str(summary_text),
        assistant_text=None,
        entities_json="[]",
        client_context_json=common_utils.json_dumps(
            {
                "intent_id": str(intent.intent_id),
                "decision_id": str(decision_id),
                "action_type": str(action_type),
                "capability": str(capability_name),
                "result_status": str(result_status),
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
            decision_id=str(decision_id),
            capability_name=str(capability_name),
            result_status=str(result_status),
            result_payload_json=str(result_payload_json),
            summary_text=str(summary_text),
            useful_for_recall_hint=int(useful_for_recall_hint),
            recall_decision=-1,
            recall_decided_at=None,
            created_at=int(now_system_ts),
        )
    )
    # --- world_model_items が source_result_id(FK) を参照するため、親行を先に永続化する ---
    db.flush()

    # --- world_model_items へ集約（対象外 capability は no-op） ---
    _upsert_world_model_item_from_action_result(
        db=db,
        source_event_id=int(result_event.event_id),
        source_result_id=str(result_id),
        capability_name=str(capability_name),
        result_status=str(result_status),
        summary_text=str(summary_text),
        result_payload_json=str(result_payload_json),
        now_system_ts=int(now_system_ts),
        now_domain_ts=int(now_domain_ts),
    )

    # --- intent 状態を終端へ遷移 ---
    if str(result_status) == "failed":
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
                "dropped_reason": str(summary_text)[:1000],
                "dropped_at": int(now_domain_ts),
                "last_result_status": str(result_status),
                "updated_at": int(now_system_ts),
                "intent_id": str(intent.intent_id),
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
                "last_result_status": str(result_status),
                "updated_at": int(now_system_ts),
                "intent_id": str(intent.intent_id),
            },
        )

        # --- goal は成功/部分成功で完了に寄せる ---
        goal_id_for_update = goal_id_hint
        if goal_id_for_update is None and intent.goal_id is not None and str(intent.goal_id).strip():
            goal_id_for_update = str(intent.goal_id)
        if goal_id_for_update is not None and str(goal_id_for_update).strip():
            db.execute(
                text(
                    """
                    UPDATE goals
                       SET status='done',
                           updated_at=:updated_at
                     WHERE goal_id=:goal_id
                       AND status <> 'done'
                       AND status <> 'dropped'
                    """
                ),
                {
                    "updated_at": int(now_system_ts),
                    "goal_id": str(goal_id_for_update),
                },
            )

    # --- next_trigger があれば投入 ---
    if isinstance(next_trigger, dict) and bool(next_trigger.get("required")):
        trigger_type = str(next_trigger.get("trigger_type") or "event")
        trigger_key = str(next_trigger.get("trigger_key") or f"next:{str(result_id)}")
        trigger_payload = next_trigger.get("payload") if isinstance(next_trigger.get("payload"), dict) else {}
        trigger_scheduled_at_raw = next_trigger.get("scheduled_at")
        trigger_scheduled_at = (
            int(trigger_scheduled_at_raw) if trigger_scheduled_at_raw is not None else int(now_domain_ts)
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

    # --- 後続ジョブを投入 ---
    _enqueue_standard_post_action_jobs(
        db=db,
        event_id=int(result_event.event_id),
        result_id=str(result_id),
        now_system_ts=int(now_system_ts),
    )
    return str(result_id)


def _collect_deliberation_input(
    db,
    *,
    trigger: AutonomyTrigger,
    persona_text: str,
    addon_text: str,
    second_person_label: str,
) -> dict[str, Any]:
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
            "payload": common_utils.json_loads_maybe(str(s.payload_json or "")),
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
            "target_condition": common_utils.json_loads_maybe(str(r[5] or "{}")),
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

    # --- Deliberation入力には内部追跡用UUIDを出さない（evidence IDsとの混同を防ぐ） ---
    pt = str(persona_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    at = str(addon_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    sp = str(second_person_label or "").strip() or "あなた"
    return {
        "trigger": {
            "trigger_type": str(trigger.trigger_type),
            "trigger_key": str(trigger.trigger_key),
            "source_event_id": (int(trigger.source_event_id) if trigger.source_event_id is not None else None),
            "payload": trigger_payload,
            "scheduled_at": (int(trigger.scheduled_at) if trigger.scheduled_at is not None else None),
        },
        "persona": {
            "second_person_label": str(sp),
            "persona_text": str(pt),
            "addon_text": str(at),
        },
        "events": event_rows,
        "states": state_rows,
        "goals": goals,
        "intents": intents,
        "mood": _load_recent_mood_snapshot(db),
        "capabilities": [
            {
                "capability": "vision_perception",
                "action_types": ["observe_screen", "observe_camera"],
            },
            {
                "capability": "web_access",
                "action_types": ["web_research"],
            },
            {
                "capability": "schedule_alarm",
                "action_types": ["schedule_action"],
            },
            {
                "capability": "device_control",
                "action_types": ["device_action"],
            },
            {
                "capability": "mobility_move",
                "action_types": ["move_to"],
            },
            {
                "capability": "agent_delegate",
                "action_types": ["agent_delegate"],
                "backends": ["codex"],
            },
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
            cfg = get_config_store().config
            deliberation_input = _collect_deliberation_input(
                db,
                trigger=trigger,
                persona_text=str(cfg.persona_text),
                addon_text=str(cfg.addon_text),
                second_person_label=str(cfg.second_person_label),
            )
            system_prompt = prompt_builders.autonomy_deliberation_system_prompt(
                second_person_label=str(cfg.second_person_label),
            )

            # --- LLMで意思決定 ---
            resp = llm_client.generate_json_response(
                system_prompt=system_prompt,
                input_text=common_utils.json_dumps(deliberation_input),
                purpose=LlmRequestPurpose.ASYNC_AUTONOMY_DELIBERATION,
                max_tokens=int(cfg.max_tokens),
            )
            # --- LLM出力不正（JSON崩れ/契約違反）は業務エラーとして drop 扱いにする ---
            try:
                decision_raw = llm_client.response_json(resp)
            except ValueError as exc:
                raise _DeliberationInvalidOutputError(
                    drop_reason="deliberation_invalid_json",
                    detail=str(exc),
                ) from exc
            if not isinstance(decision_raw, dict):
                raise _DeliberationInvalidOutputError(
                    drop_reason="deliberation_invalid_json",
                    detail="deliberation output is not a JSON object",
                )
            try:
                decision = parse_action_decision(decision_raw)
            except ValueError as exc:
                raise _DeliberationInvalidOutputError(
                    drop_reason="deliberation_invalid_contract",
                    detail=str(exc),
                ) from exc

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

            # --- 記憶更新（WritePlan）ジョブを投入（decision は searchable=0 でも対象） ---
            db.add(
                Job(
                    kind="generate_write_plan",
                    payload_json=common_utils.json_dumps({"event_id": int(decision_event.event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_system_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_system_ts),
                    updated_at=int(now_system_ts),
                )
            )

            # --- decision_outcome=do_action: Intent を冪等生成して execute_intent を投入 ---
            if str(decision.decision_outcome) == "do_action":
                goal_id_for_intent = _maybe_create_goal_from_decision(
                    db=db,
                    decision_id=str(decision_id),
                    action_type=str(decision.action_type or ""),
                    action_payload_json=(str(decision.action_payload_json) if decision.action_payload_json else None),
                    priority=int(decision.priority),
                    now_system_ts=int(now_system_ts),
                )

                # --- 親行（action_decisions / goals）を先に flush する ---
                # `intents` は `decision_id` / `goal_id` の外部キーを持つ。
                # ここで生SQL `INSERT` を使うため、ORM の pending 行を明示 flush して
                # FK親を先にDBへ反映してから `intents` を追加する。
                db.flush()

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
                            :goal_id,
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
                        "goal_id": (str(goal_id_for_intent) if goal_id_for_intent else None),
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
    except _DeliberationInvalidOutputError as exc:
        # --- 想定内のLLM出力不正は warning に落として trigger を drop する ---
        # --- ログ上で JSON系の異常を見分けやすくするため、分類ラベルを付ける ---
        anomaly_label = "JSON異常(形式不整合)"
        if str(exc.drop_reason) == "deliberation_invalid_json":
            anomaly_label = "JSON異常(パース)"
        logger.warning(
            "deliberate_once dropped invalid LLM output [%s] trigger_id=%s reason=%s detail=%s",
            str(anomaly_label),
            str(trigger_id),
            str(exc.drop_reason),
            str(exc.detail),
        )
        _mark_trigger_dropped(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            trigger_id=str(trigger_id),
            claim_token=str(claim_token),
            now_system_ts=int(now_system_ts),
            reason=str(exc.drop_reason),
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

    cfg = get_config_store().config
    # --- claim後に必要な情報を保持（Capability実行はDB外で行う） ---
    claimed_action_type = ""
    claimed_decision_id = ""
    claimed_goal_id: str | None = None
    intent_payload: dict[str, Any] = {}

    try:
        # --- Phase 1: queued -> running claim と読み取り（短いDBトランザクション） ---
        now_system_ts = _now_utc_ts()
        now_domain_ts = _now_domain_utc_ts()
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
                       AND (
                            SELECT COUNT(*)
                              FROM intents AS i2
                             WHERE i2.status='running'
                               AND i2.action_type <> 'agent_delegate'
                       ) < :max_parallel
                    """
                ),
                {
                    "updated_at": int(now_system_ts),
                    "intent_id": str(intent_id),
                    "max_parallel": max(1, int(cfg.autonomy_max_parallel_intents)),
                },
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

            # --- Capability実行に必要な値だけ保持してDBを閉じる ---
            claimed_action_type = str(intent.action_type or "")
            claimed_decision_id = str(decision.decision_id)
            claimed_goal_id = (str(intent.goal_id) if intent.goal_id is not None and str(intent.goal_id).strip() else None)
            intent_payload_raw = common_utils.json_loads_maybe(str(intent.action_payload_json or ""))
            intent_payload = dict(intent_payload_raw) if isinstance(intent_payload_raw, dict) else {}

        # --- Phase 2: capability 実行（DBロックを持たない） ---
        is_agent_delegate = False
        if str(claimed_action_type) == "web_research":
            result = execute_web_research(
                llm_client=llm_client,
                second_person_label=str(cfg.second_person_label),
                query=str(intent_payload.get("query") or ""),
                goal=str(intent_payload.get("goal") or ""),
                constraints=[str(x) for x in list(intent_payload.get("constraints") or [])],
            )
            capability_name = "web_access"
        elif str(claimed_action_type) in {"observe_screen", "observe_camera"}:
            result = execute_vision_perception(
                llm_client=llm_client,
                action_type=str(claimed_action_type),
                action_payload=intent_payload,
            )
            capability_name = "vision_perception"
        elif str(claimed_action_type) == "schedule_action":
            result = execute_schedule_alarm(
                action_payload=intent_payload,
            )
            capability_name = "schedule_alarm"
        elif str(claimed_action_type) == "device_action":
            result = execute_device_control(
                action_payload=intent_payload,
            )
            capability_name = "device_control"
        elif str(claimed_action_type) == "move_to":
            result = execute_mobility_move(
                action_payload=intent_payload,
            )
            capability_name = "mobility_move"
        elif str(claimed_action_type) == "agent_delegate":
            # --- 汎用委譲は callback 完了時に ActionResult を保存するため、ここでは job 作成だけ行う ---
            backend = str(intent_payload.get("backend") or "").strip()
            task_instruction = str(intent_payload.get("task_instruction") or "").strip()
            if not backend:
                raise RuntimeError("agent_delegate requires action_payload.backend")
            if not task_instruction:
                raise RuntimeError("agent_delegate requires action_payload.task_instruction")
            capability_name = "agent_delegate"
            is_agent_delegate = True
            result = None
        else:
            capability_name = "unknown"
            result = CapabilityExecutionResult(
                result_status="failed",
                summary=f"unsupported action_type: {str(claimed_action_type)}",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": str(claimed_action_type),
                        "action_payload": intent_payload,
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )

        # --- Phase 3: 結果保存（別トランザクション） ---
        now_system_ts = _now_utc_ts()
        now_domain_ts = _now_domain_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- running 以外に遷移済みなら二重保存しない ---
            intent = db.query(Intent).filter(Intent.intent_id == str(intent_id)).one_or_none()
            if intent is None:
                return
            if str(intent.status) != "running":
                return

            if bool(is_agent_delegate):
                # --- agent_delegate は別プロセス runner 向け job を作成し、Intent は running のまま維持 ---
                existing_agent_job = db.query(AgentJob).filter(AgentJob.intent_id == str(intent_id)).one_or_none()
                if existing_agent_job is None:
                    db.add(
                        AgentJob(
                            job_id=str(uuid.uuid4()),
                            intent_id=str(intent_id),
                            decision_id=str(claimed_decision_id),
                            backend=str(intent_payload.get("backend") or ""),
                            task_instruction=str(intent_payload.get("task_instruction") or ""),
                            status="queued",
                            claim_token=None,
                            runner_id=None,
                            attempts=0,
                            heartbeat_at=None,
                            result_status=None,
                            result_summary_text=None,
                            result_details_json="{}",
                            error_code=None,
                            error_message=None,
                            created_at=int(now_system_ts),
                            started_at=None,
                            finished_at=None,
                            updated_at=int(now_system_ts),
                        )
                    )
                return

            if not isinstance(result, CapabilityExecutionResult):
                raise RuntimeError("execute_intent internal error: capability result missing")

            _finalize_intent_and_save_action_result(
                db=db,
                intent=intent,
                decision_id=str(claimed_decision_id),
                action_type=str(claimed_action_type),
                capability_name=str(capability_name),
                result_status=str(result.result_status),
                summary_text=str(result.summary),
                result_payload_json=str(result.result_payload_json),
                useful_for_recall_hint=int(result.useful_for_recall_hint),
                now_system_ts=int(now_system_ts),
                now_domain_ts=int(now_domain_ts),
                goal_id_hint=(str(claimed_goal_id) if claimed_goal_id is not None else None),
                next_trigger=result.next_trigger,
            )
    except Exception as exc:  # noqa: BLE001
        # --- 途中失敗時は running intent の取り残しを防ぐ ---
        fail_reason = str(exc or "").strip() or "execute_intent_failed"
        if len(fail_reason) > 1000:
            fail_reason = fail_reason[:1000]
        try:
            fail_system_ts = _now_utc_ts()
            fail_domain_ts = _now_domain_utc_ts()
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                db.execute(
                    text(
                        """
                        UPDATE intents
                           SET status='dropped',
                               dropped_reason=:dropped_reason,
                               dropped_at=:dropped_at,
                               updated_at=:updated_at
                         WHERE intent_id=:intent_id
                           AND status='running'
                        """
                    ),
                    {
                        "dropped_reason": str(fail_reason),
                        "dropped_at": int(fail_domain_ts),
                        "updated_at": int(fail_system_ts),
                        "intent_id": str(intent_id),
                    },
                )
        except Exception:  # noqa: BLE001
            logger.exception("execute_intent cleanup failed intent_id=%s", str(intent_id))
        raise

def complete_agent_job_from_runner(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    job_id: str,
    claim_token: str,
    runner_id: str,
    result_status: str,
    summary_text: str,
    details_json_obj: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    agent_runner の complete callback を処理し、ActionResult/Event/Intent を終端化する。
    """

    # --- 入力を正規化 ---
    job_id_norm = str(job_id or "").strip()
    token_norm = str(claim_token or "").strip()
    runner_id_norm = str(runner_id or "").strip()
    result_status_norm = str(result_status or "").strip()
    summary_norm = str(summary_text or "").strip()
    details_obj = dict(details_json_obj or {})
    if not job_id_norm or not token_norm or not runner_id_norm:
        raise RuntimeError("job_id / claim_token / runner_id are required")
    if result_status_norm not in {"success", "partial", "no_effect"}:
        raise RuntimeError("result_status must be success/partial/no_effect for complete")
    if not summary_norm:
        summary_norm = "agent job completed"

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- callback 対象 job を検証 ---
        job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id_norm)).one_or_none()
        if job is None:
            raise RuntimeError("agent_job not found")
        if str(job.claim_token or "") != str(token_norm):
            raise RuntimeError("agent_job claim_token mismatch")
        if str(job.runner_id or "") != str(runner_id_norm):
            raise RuntimeError("agent_job runner_id mismatch")
        if str(job.status) in {"completed", "failed", "cancelled", "timed_out"}:
            return {
                "job_id": str(job.job_id),
                "status": str(job.status),
                "intent_id": str(job.intent_id),
                "backend": str(job.backend),
            }
        if str(job.status) not in {"claimed", "running"}:
            raise RuntimeError(f"agent_job status is not claimable for completion: {str(job.status)}")

        # --- 対応 intent を検証（running のまま維持されている前提） ---
        intent = db.query(Intent).filter(Intent.intent_id == str(job.intent_id)).one_or_none()
        if intent is None:
            raise RuntimeError("intent not found for agent_job")
        if str(intent.status) != "running":
            raise RuntimeError(f"intent status is not running: {str(intent.status)}")

        # --- job を completed に更新 ---
        job.status = "completed"
        job.result_status = str(result_status_norm)
        job.result_summary_text = str(summary_norm)
        job.result_details_json = common_utils.json_dumps(details_obj)
        job.error_code = None
        job.error_message = None
        job.started_at = int(job.started_at or now_system_ts)
        job.finished_at = int(now_system_ts)
        job.heartbeat_at = int(now_system_ts)
        job.updated_at = int(now_system_ts)

        # --- result payload は agent job 文脈付きで保存する ---
        result_payload_obj = {
            "agent_job_id": str(job.job_id),
            "backend": str(job.backend),
            "task_instruction": str(job.task_instruction),
            "details": details_obj,
        }
        useful_hint = 1 if str(result_status_norm) in {"success", "partial"} and bool(summary_norm) else 0

        _finalize_intent_and_save_action_result(
            db=db,
            intent=intent,
            decision_id=str(job.decision_id),
            action_type="agent_delegate",
            capability_name="agent_delegate",
            result_status=str(result_status_norm),
            summary_text=str(summary_norm),
            result_payload_json=common_utils.json_dumps(result_payload_obj),
            useful_for_recall_hint=int(useful_hint),
            now_system_ts=int(now_system_ts),
            now_domain_ts=int(now_domain_ts),
            goal_id_hint=None,
            next_trigger=None,
        )
        return {
            "job_id": str(job.job_id),
            "status": str(job.status),
            "intent_id": str(job.intent_id),
            "backend": str(job.backend),
        }


def fail_agent_job_from_runner(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    job_id: str,
    claim_token: str,
    runner_id: str,
    error_code: str | None,
    error_message: str,
) -> dict[str, Any]:
    """
    agent_runner の fail callback を処理し、ActionResult/Event/Intent を失敗終端化する。
    """

    # --- 入力を正規化 ---
    job_id_norm = str(job_id or "").strip()
    token_norm = str(claim_token or "").strip()
    runner_id_norm = str(runner_id or "").strip()
    error_code_norm = str(error_code or "").strip() or "agent_job_failed"
    error_message_norm = str(error_message or "").strip() or "agent job failed"
    if len(error_message_norm) > 1000:
        error_message_norm = error_message_norm[:1000]
    if not job_id_norm or not token_norm or not runner_id_norm:
        raise RuntimeError("job_id / claim_token / runner_id are required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- callback 対象 job を検証 ---
        job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id_norm)).one_or_none()
        if job is None:
            raise RuntimeError("agent_job not found")
        if str(job.claim_token or "") != str(token_norm):
            raise RuntimeError("agent_job claim_token mismatch")
        if str(job.runner_id or "") != str(runner_id_norm):
            raise RuntimeError("agent_job runner_id mismatch")
        if str(job.status) in {"completed", "failed", "cancelled", "timed_out"}:
            return {
                "job_id": str(job.job_id),
                "status": str(job.status),
                "intent_id": str(job.intent_id),
                "backend": str(job.backend),
            }
        if str(job.status) not in {"claimed", "running"}:
            raise RuntimeError(f"agent_job status is not claimable for failure: {str(job.status)}")

        # --- 対応 intent を検証 ---
        intent = db.query(Intent).filter(Intent.intent_id == str(job.intent_id)).one_or_none()
        if intent is None:
            raise RuntimeError("intent not found for agent_job")
        if str(intent.status) != "running":
            raise RuntimeError(f"intent status is not running: {str(intent.status)}")

        # --- job を failed に更新 ---
        job.status = "failed"
        job.result_status = "failed"
        job.result_summary_text = str(error_message_norm)
        job.result_details_json = common_utils.json_dumps(
            {
                "agent_job_id": str(job.job_id),
                "backend": str(job.backend),
                "error_code": str(error_code_norm),
                "error_message": str(error_message_norm),
            }
        )
        job.error_code = str(error_code_norm)
        job.error_message = str(error_message_norm)
        job.started_at = int(job.started_at or now_system_ts)
        job.finished_at = int(now_system_ts)
        job.heartbeat_at = int(now_system_ts)
        job.updated_at = int(now_system_ts)

        _finalize_intent_and_save_action_result(
            db=db,
            intent=intent,
            decision_id=str(job.decision_id),
            action_type="agent_delegate",
            capability_name="agent_delegate",
            result_status="failed",
            summary_text=str(error_message_norm),
            result_payload_json=str(job.result_details_json),
            useful_for_recall_hint=0,
            now_system_ts=int(now_system_ts),
            now_domain_ts=int(now_domain_ts),
            goal_id_hint=None,
            next_trigger=None,
        )
        return {
            "job_id": str(job.job_id),
            "status": str(job.status),
            "intent_id": str(job.intent_id),
            "backend": str(job.backend),
        }


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


def _handle_sweep_agent_jobs(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    payload: dict[str, Any],
) -> None:
    """stale な agent_jobs を timed_out/failed として終端化する。"""

    # --- payload から閾値/上限を読む（未指定時は定数既定） ---
    stale_seconds_raw = payload.get("stale_seconds")
    sweep_limit_raw = payload.get("limit")
    stale_seconds = max(
        1,
        int(stale_seconds_raw if stale_seconds_raw is not None else _AGENT_JOB_STALE_SECONDS),
    )
    sweep_limit = max(1, int(sweep_limit_raw if sweep_limit_raw is not None else 64))

    # --- stale 判定基準時刻 ---
    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    stale_before_ts = int(now_system_ts) - int(stale_seconds)

    timed_out_jobs = 0
    finalized_intents = 0
    skipped_jobs = 0

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- stale 候補（claimed/running）を古い heartbeat 順で取得 ---
        rows = db.execute(
            text(
                """
                SELECT job_id
                  FROM agent_jobs
                 WHERE status IN ('claimed','running')
                   AND COALESCE(heartbeat_at, updated_at, created_at) <= :stale_before_ts
                 ORDER BY COALESCE(heartbeat_at, updated_at, created_at) ASC, created_at ASC
                 LIMIT :limit
                """
            ),
            {
                "stale_before_ts": int(stale_before_ts),
                "limit": int(sweep_limit),
            },
        ).fetchall()

        # --- 各 stale job を timeout 終端化 ---
        for row in rows:
            job_id = str((row[0] if row else "") or "").strip()
            if not job_id:
                continue

            job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id)).one_or_none()
            if job is None:
                continue
            if str(job.status) not in {"claimed", "running"}:
                continue

            # --- job を timed_out に遷移 ---
            timeout_message = (
                f"agent runner heartbeat timeout (backend={str(job.backend)}, stale_seconds={int(stale_seconds)})"
            )
            timeout_details = {
                "agent_job_id": str(job.job_id),
                "backend": str(job.backend),
                "error_code": "agent_job_timeout",
                "error_message": str(timeout_message),
                "stale_seconds": int(stale_seconds),
                "runner_id": (str(job.runner_id) if job.runner_id is not None else None),
                "last_heartbeat_at": (int(job.heartbeat_at) if job.heartbeat_at is not None else None),
            }
            job.status = "timed_out"
            job.result_status = "failed"
            job.result_summary_text = str(timeout_message)
            job.result_details_json = common_utils.json_dumps(timeout_details)
            job.error_code = "agent_job_timeout"
            job.error_message = str(timeout_message)
            job.started_at = int(job.started_at or now_system_ts)
            job.finished_at = int(now_system_ts)
            job.updated_at = int(now_system_ts)
            timed_out_jobs += 1

            # --- 対応 intent が running のときだけ ActionResult を保存して終端化 ---
            intent = db.query(Intent).filter(Intent.intent_id == str(job.intent_id)).one_or_none()
            if intent is None or str(intent.status) != "running":
                skipped_jobs += 1
                continue

            _finalize_intent_and_save_action_result(
                db=db,
                intent=intent,
                decision_id=str(job.decision_id),
                action_type="agent_delegate",
                capability_name="agent_delegate",
                result_status="failed",
                summary_text=str(timeout_message),
                result_payload_json=str(job.result_details_json or "{}"),
                useful_for_recall_hint=0,
                now_system_ts=int(now_system_ts),
                now_domain_ts=int(now_domain_ts),
                goal_id_hint=None,
                next_trigger=None,
            )
            finalized_intents += 1

    # --- 実際に回収が発生したときだけ warning を出す ---
    if int(timed_out_jobs) > 0:
        logger.warning(
            "sweep_agent_jobs timed out stale agent jobs timed_out=%s finalized_intents=%s skipped_jobs=%s stale_seconds=%s",
            int(timed_out_jobs),
            int(finalized_intents),
            int(skipped_jobs),
            int(stale_seconds),
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

        # --- RAM blackboard へも反映（DB snapshot と同じ内容を保持） ---
        get_runtime_blackboard().apply_snapshot_payload(
            snapshot_kind=str(snapshot_kind),
            payload=dict(snapshot_payload),
            created_at=int(now_system_ts),
        )

        db.add(
            RuntimeSnapshot(
                snapshot_kind=str(snapshot_kind),
                payload_json=common_utils.json_dumps(snapshot_payload),
                created_at=int(now_system_ts),
            )
        )

        # --- agent_jobs stale 回収ジョブを重複なく投入 ---
        existing_sweep = (
            db.query(Job.job_id)
            .filter(Job.kind == "sweep_agent_jobs")
            .filter(Job.status.in_([int(_JOB_PENDING), int(_JOB_RUNNING)]))
            .first()
        )
        if existing_sweep is None:
            db.add(
                Job(
                    kind="sweep_agent_jobs",
                    payload_json=common_utils.json_dumps(
                        {
                            "source": "snapshot_runtime",
                            "stale_seconds": int(_AGENT_JOB_STALE_SECONDS),
                            "limit": 64,
                        }
                    ),
                    status=int(_JOB_PENDING),
                    run_after=int(now_system_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_system_ts),
                    updated_at=int(now_system_ts),
                )
            )
