"""
気分（LongMoodState）デバッグAPI

提供する機能:
    - 現在の long_mood_state を、観測/デバッグ向けに取得する。
    - 直近の event_affects（瞬間感情）を、観測/デバッグ向けに取得する。

注意:
    - 運用前のため互換は付けない（仕様変更は許容）。
    - shock は「余韻」なので、読み出し時点で時間減衰させた値（shock_vad）を返す。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func

from cocoro_ghost.core import affect
from cocoro_ghost.jobs import runner as worker
from cocoro_ghost.autonomy.contracts import (
    derive_report_candidate_for_action_result,
    parse_console_delivery,
    resolve_delivery_mode_from_report_candidate_level,
)
from cocoro_ghost.core.clock import ClockService
from cocoro_ghost.core import common_utils
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.storage.db import memory_session_scope
from cocoro_ghost.app_bootstrap.dependencies import get_clock_service_dep, get_config_store_dep
from cocoro_ghost.autonomy.runtime_blackboard import get_runtime_blackboard
from cocoro_ghost.storage.memory_models import (
    AgendaThread,
    ActionDecision,
    ActionResult,
    AgentJob,
    AutonomyTrigger,
    Event,
    EventAffect,
    Intent,
    Job,
    State,
)
from cocoro_ghost.core.time_utils import format_iso8601_local_with_tz
from cocoro_ghost.jobs.constants import AGENT_JOB_STALE_SECONDS as _AGENT_JOB_STALE_SECONDS
from cocoro_ghost.jobs.constants import JOB_PENDING as _JOB_PENDING
from cocoro_ghost.jobs.constants import JOB_RUNNING as _JOB_RUNNING


router = APIRouter(prefix="/mood", tags=["mood"])

# --- デバッグ用: 直近event_affectの件数（固定） ---
# NOTE:
# - クエリパラメータで増減させると互換や運用が複雑になるため、サーバ側で固定する。
# - まずは「直近の揺れ」を見る用途なので、少数（例: 8）で十分。
_RECENT_EVENT_AFFECTS_LIMIT = 8
_RECENT_INTENTS_LIMIT = 8
_RECENT_AGENT_JOBS_LIMIT = 8
_RUNTIME_ATTENTION_TARGETS_LIMIT = 8
_CURRENT_THOUGHT_TARGETS_LIMIT = 8
_AGENDA_THREADS_LIMIT = 8


def _fmt_ts_or_none(ts: int | None) -> str | None:
    """UNIX秒をローカルISO文字列へ変換する（NoneはNone）。"""

    # --- null はそのまま返す ---
    if ts is None:
        return None

    # --- フォーマット失敗は例外化してデバッグAPIの不整合を表面化する ---
    return format_iso8601_local_with_tz(int(ts))


def _build_success_delivery_expectation_for_action_type(*, action_type: str) -> dict[str, Any]:
    """intent の成功時配信見込みを返す。"""

    # --- web_research は result_payload の量で共有優先度が変わる ---
    action_type_norm = str(action_type or "").strip()
    if action_type_norm == "web_research":
        return {
            "report_candidate_level": None,
            "delivery_mode": None,
            "note": "result_payload_dependent",
        }

    # --- それ以外は構造値だけで決められる ---
    report_candidate = derive_report_candidate_for_action_result(
        action_type=str(action_type_norm),
        result_status="success",
    )
    return {
        "report_candidate_level": str(report_candidate["level"]),
        "delivery_mode": resolve_delivery_mode_from_report_candidate_level(
            str(report_candidate["level"]),
        ),
        "note": None,
    }


def _build_background_debug_snapshot(
    *,
    db,
    config_store: ConfigStore,
    now_domain_ts: int,
) -> dict[str, Any]:
    """
    感情デバッグ画面向けのバックグラウンド状態スナップショットを返す。

    方針:
        - 感情デバッグAPIの既存ポーリングを流用し、追加APIを増やさない。
        - 監視に必要な構造情報だけ返す（本文の意味推定はしない）。
        - /api/control 系の完全代替ではなく、デバッグ窓で見たい要約を返す。
    """

    # --- アクティブ設定（Ghostの現在設定） ---
    cfg = config_store.config
    embedding_preset_id = str(cfg.embedding_preset_id).strip()
    embedding_dimension = int(cfg.embedding_dimension)
    autonomy_enabled = bool(getattr(cfg, "autonomy_enabled", False))
    heartbeat_seconds = int(getattr(cfg, "autonomy_heartbeat_seconds", 30))
    max_parallel_intents = max(1, int(getattr(cfg, "autonomy_max_parallel_intents", 2)))
    trigger_claim_limit_per_tick = max(1, int(max_parallel_intents) * 2)

    # --- worker jobs の統計（memory jobs テーブル） ---
    worker_stats = worker.get_job_queue_stats(
        embedding_preset_id=str(embedding_preset_id),
        embedding_dimension=int(embedding_dimension),
    )
    now_system_ts = int(time.time())

    # --- autonomy triggers/intents の滞留数（同一DBセッションで取得） ---
    triggers_queued = int(
        db.query(func.count(AutonomyTrigger.trigger_id))
        .filter(AutonomyTrigger.status == "queued")
        .scalar()
        or 0
    )
    triggers_claimed = int(
        db.query(func.count(AutonomyTrigger.trigger_id))
        .filter(AutonomyTrigger.status == "claimed")
        .scalar()
        or 0
    )
    triggers_due = int(
        db.query(func.count(AutonomyTrigger.trigger_id))
        .filter(AutonomyTrigger.status == "queued")
        .filter(
            (AutonomyTrigger.scheduled_at.is_(None)) | (AutonomyTrigger.scheduled_at <= int(now_domain_ts))
        )
        .scalar()
        or 0
    )
    intents_queued = int(db.query(func.count(Intent.intent_id)).filter(Intent.status == "queued").scalar() or 0)
    intents_running = int(db.query(func.count(Intent.intent_id)).filter(Intent.status == "running").scalar() or 0)
    intents_blocked = int(db.query(func.count(Intent.intent_id)).filter(Intent.status == "blocked").scalar() or 0)
    intents_running_non_delegate = int(
        db.query(func.count(Intent.intent_id))
        .filter(Intent.status == "running")
        .filter(Intent.action_type != "agent_delegate")
        .scalar()
        or 0
    )

    # --- deliberate_once / execute_intent の workerジョブ滞留 ---
    deliberate_jobs_pending_due = int(
        db.query(func.count(Job.id))
        .filter(Job.kind == "deliberate_once")
        .filter(Job.status == int(_JOB_PENDING))
        .filter(Job.run_after <= int(now_system_ts))
        .scalar()
        or 0
    )
    deliberate_jobs_pending_future = int(
        db.query(func.count(Job.id))
        .filter(Job.kind == "deliberate_once")
        .filter(Job.status == int(_JOB_PENDING))
        .filter(Job.run_after > int(now_system_ts))
        .scalar()
        or 0
    )
    deliberate_jobs_running = int(
        db.query(func.count(Job.id))
        .filter(Job.kind == "deliberate_once")
        .filter(Job.status == int(_JOB_RUNNING))
        .scalar()
        or 0
    )
    execute_jobs_pending_due = int(
        db.query(func.count(Job.id))
        .filter(Job.kind == "execute_intent")
        .filter(Job.status == int(_JOB_PENDING))
        .filter(Job.run_after <= int(now_system_ts))
        .scalar()
        or 0
    )
    execute_jobs_pending_future = int(
        db.query(func.count(Job.id))
        .filter(Job.kind == "execute_intent")
        .filter(Job.status == int(_JOB_PENDING))
        .filter(Job.run_after > int(now_system_ts))
        .scalar()
        or 0
    )
    execute_jobs_running = int(
        db.query(func.count(Job.id))
        .filter(Job.kind == "execute_intent")
        .filter(Job.status == int(_JOB_RUNNING))
        .scalar()
        or 0
    )

    # --- 直近意思決定（do_action / skip / defer） ---
    latest_decision_row = (
        db.query(ActionDecision)
        .order_by(ActionDecision.created_at.desc())
        .limit(1)
        .one_or_none()
    )
    latest_decision: dict[str, Any] | None = None
    if latest_decision_row is not None:
        # --- latest decision の progress 表示方針を読む ---
        console_delivery_obj = parse_console_delivery(
            common_utils.json_loads_maybe(str(latest_decision_row.console_delivery_json or "{}"))
        )

        # --- latest decision に対応する最新 action_result を引く ---
        latest_result_row = (
            db.query(ActionResult)
            .filter(ActionResult.decision_id == str(latest_decision_row.decision_id))
            .order_by(ActionResult.created_at.desc())
            .limit(1)
            .one_or_none()
        )
        completion_result_status = None
        completion_report_candidate_level = None
        completion_delivery_mode = None
        completion_delivery_reason = None
        if latest_result_row is not None:
            completion_result_status = str(latest_result_row.result_status)
            completion_result_payload_obj = common_utils.json_loads_maybe(str(latest_result_row.result_payload_json or "{}"))
            if not isinstance(completion_result_payload_obj, dict):
                raise RuntimeError("action_result.result_payload_json is not an object")
            completion_report_candidate = derive_report_candidate_for_action_result(
                action_type=(str(latest_decision_row.action_type) if latest_decision_row.action_type else ""),
                result_status=str(latest_result_row.result_status),
                result_payload=dict(completion_result_payload_obj),
            )
            completion_report_candidate_level = str(completion_report_candidate["level"])
            completion_delivery_mode = resolve_delivery_mode_from_report_candidate_level(
                completion_report_candidate_level,
            )
            completion_delivery_reason = str(completion_report_candidate["reason"] or "").strip() or None

        # --- latest decision の理由本文はプレビューだけ返す ---
        reason_preview = str(latest_decision_row.reason_text or "").strip()
        if len(reason_preview) > 160:
            reason_preview = reason_preview[:160]
        latest_decision = {
            "decision_id": str(latest_decision_row.decision_id),
            "decision_outcome": str(latest_decision_row.decision_outcome),
            "trigger_type": str(latest_decision_row.trigger_type),
            "action_type": (str(latest_decision_row.action_type) if latest_decision_row.action_type else None),
            "progress_delivery_mode": str(console_delivery_obj.get("on_progress") or ""),
            "progress_message_kind": str(console_delivery_obj.get("message_kind") or ""),
            "result_status": completion_result_status,
            "completion_report_candidate_level": completion_report_candidate_level,
            "completion_delivery_mode": completion_delivery_mode,
            "completion_delivery_reason": completion_delivery_reason,
            "reason_text_preview": (str(reason_preview) if reason_preview else None),
            "defer_until": _fmt_ts_or_none(int(latest_decision_row.defer_until)) if latest_decision_row.defer_until is not None else None,
            "next_deliberation_at": (
                _fmt_ts_or_none(int(latest_decision_row.next_deliberation_at))
                if latest_decision_row.next_deliberation_at is not None
                else None
            ),
            "created_at": _fmt_ts_or_none(int(latest_decision_row.created_at)),
        }

    # --- 直近1時間の意思決定内訳 ---
    decisions_since_ts = int(now_system_ts) - 3600
    recent_decisions_1h = {
        "do_action": 0,
        "skip": 0,
        "defer": 0,
    }
    recent_decision_rows = (
        db.query(ActionDecision.decision_outcome, func.count(ActionDecision.decision_id))
        .filter(ActionDecision.created_at >= int(decisions_since_ts))
        .group_by(ActionDecision.decision_outcome)
        .all()
    )
    for outcome, count_v in list(recent_decision_rows or []):
        key = str(outcome or "").strip()
        if key in {"do_action", "skip", "defer"}:
            recent_decisions_1h[key] = int(count_v or 0)

    # --- 判定フロー要約（どこで止まっているか） ---
    execution_slots_remaining = max(0, int(max_parallel_intents) - int(intents_running_non_delegate))
    can_start_execute_now = int(execution_slots_remaining) > 0
    decision_stage = "idle"
    decision_stage_reason = "no_due_work"
    if not bool(autonomy_enabled):
        decision_stage = "disabled"
        decision_stage_reason = "autonomy_disabled"
    elif (
        int(deliberate_jobs_pending_due) > 0
        or int(deliberate_jobs_running) > 0
        or int(triggers_claimed) > 0
    ):
        decision_stage = "deliberating"
        decision_stage_reason = "trigger_deliberation_in_progress"
    elif int(triggers_due) > 0:
        decision_stage = "waiting_deliberation_enqueue"
        decision_stage_reason = "due_trigger_waiting_for_tick"
    elif int(execute_jobs_pending_due) > 0 and not bool(can_start_execute_now):
        decision_stage = "blocked_by_parallel_limit"
        decision_stage_reason = "running_intents_reached_limit"
    elif int(execute_jobs_pending_due) > 0:
        decision_stage = "ready_to_execute"
        decision_stage_reason = "execute_job_due"
    elif int(execute_jobs_running) > 0:
        decision_stage = "executing"
        decision_stage_reason = "execute_job_running"

    # --- agent_jobs の滞留数 ---
    agent_stale_before_ts = int(now_system_ts) - int(_AGENT_JOB_STALE_SECONDS)
    agent_jobs_queued = int(db.query(func.count(AgentJob.job_id)).filter(AgentJob.status == "queued").scalar() or 0)
    agent_jobs_claimed = int(db.query(func.count(AgentJob.job_id)).filter(AgentJob.status == "claimed").scalar() or 0)
    agent_jobs_running = int(db.query(func.count(AgentJob.job_id)).filter(AgentJob.status == "running").scalar() or 0)
    agent_jobs_failed = int(db.query(func.count(AgentJob.job_id)).filter(AgentJob.status == "failed").scalar() or 0)
    agent_jobs_completed = int(db.query(func.count(AgentJob.job_id)).filter(AgentJob.status == "completed").scalar() or 0)
    agent_jobs_timed_out = int(db.query(func.count(AgentJob.job_id)).filter(AgentJob.status == "timed_out").scalar() or 0)
    agent_jobs_stale = int(
        db.query(func.count(AgentJob.job_id))
        .filter(AgentJob.status.in_(["claimed", "running"]))
        .filter(AgentJob.updated_at <= int(agent_stale_before_ts))
        .scalar()
        or 0
    )

    # --- 最近の intents（構造情報だけ返す） ---
    recent_intents_rows = (
        db.query(Intent)
        .order_by(Intent.updated_at.desc(), Intent.created_at.desc())
        .limit(int(_RECENT_INTENTS_LIMIT))
        .all()
    )
    recent_intents: list[dict[str, Any]] = []
    for row in list(recent_intents_rows or []):
        # --- intent 単位で、成功時/失敗時の完了配信を先に計算する ---
        success_expectation = _build_success_delivery_expectation_for_action_type(
            action_type=str(row.action_type),
        )
        failure_report_candidate = derive_report_candidate_for_action_result(
            action_type=str(row.action_type),
            result_status="failed",
        )

        # --- 直近結果があれば、実績の完了配信も計算する ---
        actual_completion_report_candidate_level = None
        actual_completion_delivery_mode = None
        actual_completion_delivery_reason = None
        last_result_status = str(row.last_result_status or "").strip() or None
        if last_result_status is not None:
            latest_result_row = (
                db.query(ActionResult)
                .filter(ActionResult.intent_id == str(row.intent_id))
                .order_by(ActionResult.created_at.desc())
                .limit(1)
                .one_or_none()
            )
            if latest_result_row is None:
                raise RuntimeError("intent.last_result_status is set but action_result row is missing")
            actual_result_payload_obj = common_utils.json_loads_maybe(str(latest_result_row.result_payload_json or "{}"))
            if not isinstance(actual_result_payload_obj, dict):
                raise RuntimeError("action_result.result_payload_json is not an object")
            actual_report_candidate = derive_report_candidate_for_action_result(
                action_type=str(row.action_type),
                result_status=str(last_result_status),
                result_payload=dict(actual_result_payload_obj),
            )
            actual_completion_report_candidate_level = str(actual_report_candidate["level"])
            actual_completion_delivery_mode = resolve_delivery_mode_from_report_candidate_level(
                actual_completion_report_candidate_level,
            )
            actual_completion_delivery_reason = str(actual_report_candidate["reason"] or "").strip() or None

        recent_intents.append(
            {
                "intent_id": str(row.intent_id),
                "action_type": str(row.action_type),
                "status": str(row.status),
                "priority": int(row.priority or 0),
                "goal_id": (str(row.goal_id) if row.goal_id is not None else None),
                "scheduled_at": _fmt_ts_or_none(int(row.scheduled_at) if row.scheduled_at is not None else None),
                "updated_at": _fmt_ts_or_none(int(row.updated_at)),
                "last_result_status": last_result_status,
                "success_report_candidate_level": success_expectation.get("report_candidate_level"),
                "success_delivery_mode": success_expectation.get("delivery_mode"),
                "success_delivery_note": success_expectation.get("note"),
                "failure_report_candidate_level": str(failure_report_candidate["level"]),
                "failure_delivery_mode": resolve_delivery_mode_from_report_candidate_level(
                    str(failure_report_candidate["level"])
                ),
                "actual_completion_report_candidate_level": actual_completion_report_candidate_level,
                "actual_completion_delivery_mode": actual_completion_delivery_mode,
                "actual_completion_delivery_reason": actual_completion_delivery_reason,
                "blocked_reason": (str(row.blocked_reason) if row.blocked_reason else None),
                "dropped_reason": (str(row.dropped_reason) if row.dropped_reason else None),
            }
        )

    # --- 最近の agent_jobs（構造情報だけ返す） ---
    recent_agent_job_rows = (
        db.query(AgentJob)
        .order_by(AgentJob.updated_at.desc(), AgentJob.created_at.desc())
        .limit(int(_RECENT_AGENT_JOBS_LIMIT))
        .all()
    )
    recent_agent_jobs: list[dict[str, Any]] = []
    for row in list(recent_agent_job_rows or []):
        task_instruction = str(row.task_instruction or "").strip()
        if len(task_instruction) > 200:
            task_instruction = task_instruction[:200]
        summary_text = str(row.result_summary_text or "").strip()
        if len(summary_text) > 240:
            summary_text = summary_text[:240]
        error_message = str(row.error_message or "").strip()
        if len(error_message) > 240:
            error_message = error_message[:240]
        recent_agent_jobs.append(
            {
                "job_id": str(row.job_id),
                "backend": str(row.backend),
                "status": str(row.status),
                "result_status": (str(row.result_status) if row.result_status is not None else None),
                "runner_id": (str(row.runner_id) if row.runner_id is not None else None),
                "attempts": int(row.attempts or 0),
                "task_instruction": str(task_instruction),
                "result_summary_text": (str(summary_text) if summary_text else None),
                "error_code": (str(row.error_code) if row.error_code else None),
                "error_message": (str(error_message) if error_message else None),
                "heartbeat_at": _fmt_ts_or_none(int(row.heartbeat_at) if row.heartbeat_at is not None else None),
                "updated_at": _fmt_ts_or_none(int(row.updated_at)),
            }
        )

    # --- runtime blackboard（短期状態）のRAMスナップショット ---
    runtime_bb = get_runtime_blackboard().snapshot()
    if not isinstance(runtime_bb, dict):
        runtime_bb = {}
    runtime_attention_targets_raw = runtime_bb.get("attention_targets")
    runtime_attention_targets: list[dict[str, Any]] = []
    if isinstance(runtime_attention_targets_raw, list):
        for item in list(runtime_attention_targets_raw or []):
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("type") or "").strip()
            target_value = str(item.get("value") or "").strip()
            if not target_type or not target_value:
                continue
            runtime_attention_targets.append(
                {
                    "type": str(target_type),
                    "value": str(target_value)[:120],
                    "weight": float(item.get("weight") or 0.0),
                    "updated_at": _fmt_ts_or_none(int(item.get("updated_at"))) if item.get("updated_at") is not None else None,
                }
            )
            if len(runtime_attention_targets) >= int(_RUNTIME_ATTENTION_TARGETS_LIMIT):
                break

    # --- 返却オブジェクト（感情デバッグ窓向けにまとめる） ---
    return {
        "autonomy": {
            "enabled": bool(autonomy_enabled),
            "heartbeat_seconds": int(heartbeat_seconds),
            "max_parallel_intents": int(max_parallel_intents),
            "now_domain": _fmt_ts_or_none(int(now_domain_ts)),
            "triggers": {
                "queued": int(triggers_queued),
                "claimed": int(triggers_claimed),
                "due": int(triggers_due),
            },
            "intents": {
                "queued": int(intents_queued),
                "running": int(intents_running),
                "blocked": int(intents_blocked),
            },
        },
        "decision_flow": {
            "autonomy_enabled": bool(autonomy_enabled),
            "now_system_utc": _fmt_ts_or_none(int(now_system_ts)),
            "trigger_claim_limit_per_tick": int(trigger_claim_limit_per_tick),
            "triggers_due": int(triggers_due),
            "triggers_claimed": int(triggers_claimed),
            "deliberate_once_jobs": {
                "pending_due": int(deliberate_jobs_pending_due),
                "pending_future": int(deliberate_jobs_pending_future),
                "running": int(deliberate_jobs_running),
            },
            "execute_intent_jobs": {
                "pending_due": int(execute_jobs_pending_due),
                "pending_future": int(execute_jobs_pending_future),
                "running": int(execute_jobs_running),
            },
            "running_intents_non_delegate": int(intents_running_non_delegate),
            "max_parallel_intents": int(max_parallel_intents),
            "execution_slots_remaining": int(execution_slots_remaining),
            "can_start_execute_now": bool(can_start_execute_now),
            "stage": str(decision_stage),
            "stage_reason": str(decision_stage_reason),
            "recent_decisions_1h": dict(recent_decisions_1h),
            "latest_decision": latest_decision,
        },
        "worker": {
            "pending_count": int(worker_stats.get("pending_count") or 0),
            "due_pending_count": int(worker_stats.get("due_pending_count") or 0),
            "running_count": int(worker_stats.get("running_count") or 0),
            "stale_running_count": int(worker_stats.get("stale_running_count") or 0),
            "done_count": int(worker_stats.get("done_count") or 0),
            "failed_count": int(worker_stats.get("failed_count") or 0),
            "stale_seconds": int(worker_stats.get("stale_seconds") or 0),
        },
        "agent_jobs": {
            "queued": int(agent_jobs_queued),
            "claimed": int(agent_jobs_claimed),
            "running": int(agent_jobs_running),
            "completed": int(agent_jobs_completed),
            "failed": int(agent_jobs_failed),
            "timed_out": int(agent_jobs_timed_out),
            "stale_claimed_or_running": int(agent_jobs_stale),
            "stale_seconds": int(_AGENT_JOB_STALE_SECONDS),
        },
        "runtime_blackboard": {
            "active_intent_ids": list(runtime_bb.get("active_intent_ids") or []),
            "attention_targets": list(runtime_attention_targets),
            "trigger_counts": (dict(runtime_bb.get("trigger_counts") or {}) if isinstance(runtime_bb.get("trigger_counts"), dict) else {}),
            "intent_counts": (dict(runtime_bb.get("intent_counts") or {}) if isinstance(runtime_bb.get("intent_counts"), dict) else {}),
            "worker_now_system_utc": _fmt_ts_or_none(int(runtime_bb.get("worker_now_system_utc_ts"))) if runtime_bb.get("worker_now_system_utc_ts") is not None else None,
            "last_snapshot_kind": (str(runtime_bb.get("last_snapshot_kind")) if runtime_bb.get("last_snapshot_kind") else None),
            "last_snapshot_created_at": _fmt_ts_or_none(int(runtime_bb.get("last_snapshot_created_at"))) if runtime_bb.get("last_snapshot_created_at") is not None else None,
            "last_recovered_source_snapshot_id": (
                int(runtime_bb.get("last_recovered_source_snapshot_id"))
                if runtime_bb.get("last_recovered_source_snapshot_id") is not None
                else None
            ),
            "last_recovered_system_utc": _fmt_ts_or_none(int(runtime_bb.get("last_recovered_system_utc_ts"))) if runtime_bb.get("last_recovered_system_utc_ts") is not None else None,
            "last_recovered_domain_utc": _fmt_ts_or_none(int(runtime_bb.get("last_recovered_domain_utc_ts"))) if runtime_bb.get("last_recovered_domain_utc_ts") is not None else None,
        },
        "recent_intents": list(recent_intents),
        "recent_agent_jobs": list(recent_agent_jobs),
        "limits": {
            "recent_intents_limit": int(_RECENT_INTENTS_LIMIT),
            "recent_agent_jobs_limit": int(_RECENT_AGENT_JOBS_LIMIT),
            "runtime_attention_targets_limit": int(_RUNTIME_ATTENTION_TARGETS_LIMIT),
        },
    }


def _build_current_thought_snapshot(
    *,
    db,
    now_domain_ts: int,
) -> dict[str, Any] | None:
    """
    現在の思考（current_thought_state）スナップショットを返す。

    方針:
        - 「今なにを気にしているか」を感情デバッグAPIで直接観測できるようにする。
        - payload の構造情報を優先し、本文の意味推定は行わない。
        - 更新元イベントの最小プレビューを含め、更新理由の追跡をしやすくする。
    """

    # --- 最新の current_thought_state を取得 ---
    row = (
        db.query(State)
        .filter(State.kind == "current_thought_state")
        .filter(State.searchable == 1)
        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
        .first()
    )
    if row is None:
        return None

    # --- payload を読む（壊れていても落とさない） ---
    payload_obj = common_utils.json_loads_maybe(str(row.payload_json or ""))
    if not isinstance(payload_obj, dict):
        payload_obj = {}

    # --- attention_targets（構造情報）を整形 ---
    attention_targets_raw = payload_obj.get("attention_targets")
    attention_targets: list[dict[str, Any]] = []
    total_targets_count = 0
    if isinstance(attention_targets_raw, list):
        total_targets_count = int(len(attention_targets_raw))
        for item in list(attention_targets_raw):
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("type") or "").strip()
            target_value = str(item.get("value") or "").strip()
            if not target_type or not target_value:
                continue
            updated_at_raw = item.get("updated_at")
            updated_at_iso = None
            if isinstance(updated_at_raw, (int, float)) and int(updated_at_raw) > 0:
                updated_at_iso = _fmt_ts_or_none(int(updated_at_raw))
            attention_targets.append(
                {
                    "type": str(target_type),
                    "value": str(target_value)[:120],
                    "weight": float(item.get("weight") or 0.0),
                    "updated_at": updated_at_iso,
                }
            )
            if len(attention_targets) >= int(_CURRENT_THOUGHT_TARGETS_LIMIT):
                break

    # --- updated_from（更新起点）を整形 ---
    updated_from_obj = payload_obj.get("updated_from")
    updated_from: dict[str, Any] | None = None
    updated_from_event_id = 0
    if isinstance(updated_from_obj, dict):
        try:
            updated_from_event_id = int(updated_from_obj.get("event_id") or 0)
        except Exception:  # noqa: BLE001
            updated_from_event_id = 0
        updated_from = {
            "event_id": (int(updated_from_event_id) if int(updated_from_event_id) > 0 else None),
            "event_source": str(updated_from_obj.get("event_source") or "").strip() or None,
            "action_type": str(updated_from_obj.get("action_type") or "").strip() or None,
            "capability": str(updated_from_obj.get("capability") or "").strip() or None,
            "result_status": str(updated_from_obj.get("result_status") or "").strip() or None,
        }

    # --- current_thought 固有フィールドを整形 ---
    active_thread_id = str(payload_obj.get("active_thread_id") or "").strip() or None
    focus_summary = str(payload_obj.get("focus_summary") or "").strip() or None

    next_candidate_action_raw = payload_obj.get("next_candidate_action")
    next_candidate_action: dict[str, Any] | None = None
    if isinstance(next_candidate_action_raw, dict):
        action_type = str(next_candidate_action_raw.get("action_type") or "").strip()
        payload_value = (
            dict(next_candidate_action_raw.get("payload") or {})
            if isinstance(next_candidate_action_raw.get("payload"), dict)
            else {}
        )
        if action_type:
            next_candidate_action = {
                "action_type": str(action_type),
                "payload": dict(payload_value),
            }

    talk_candidate_raw = payload_obj.get("talk_candidate")
    talk_candidate: dict[str, Any] | None = None
    if isinstance(talk_candidate_raw, dict):
        talk_level = str(talk_candidate_raw.get("level") or "").strip()
        if talk_level:
            talk_candidate = {
                "level": str(talk_level),
                "reason": str(talk_candidate_raw.get("reason") or "").strip() or None,
            }

    updated_reason = str(payload_obj.get("updated_reason") or "").strip() or None

    # --- updated_from_event_ids を整形 ---
    updated_from_event_ids_raw = payload_obj.get("updated_from_event_ids")
    updated_from_event_ids: list[int] = []
    if isinstance(updated_from_event_ids_raw, list):
        for x in list(updated_from_event_ids_raw):
            try:
                event_id_i = int(x or 0)
            except Exception:  # noqa: BLE001
                continue
            if event_id_i <= 0:
                continue
            updated_from_event_ids.append(int(event_id_i))
        updated_from_event_ids = updated_from_event_ids[-20:]

    # --- 更新元イベントのプレビュー（1件） ---
    source_event_preview: dict[str, Any] | None = None
    if int(updated_from_event_id) > 0:
        source_event_row = db.query(Event).filter(Event.event_id == int(updated_from_event_id)).one_or_none()
        if source_event_row is not None:
            source_event_preview = {
                "event_id": int(source_event_row.event_id),
                "source": str(source_event_row.source or "").strip(),
                "created_at": _fmt_ts_or_none(int(source_event_row.created_at)),
                "user_text_preview": str(source_event_row.user_text or "").strip()[:200] or None,
                "assistant_text_preview": str(source_event_row.assistant_text or "").strip()[:200] or None,
            }

    # --- 経過秒（now - last_confirmed_at） ---
    try:
        dt_seconds = int(now_domain_ts) - int(row.last_confirmed_at or 0)
    except Exception:  # noqa: BLE001
        dt_seconds = 0
    if int(dt_seconds) < 0:
        dt_seconds = 0

    # --- 返却オブジェクト ---
    return {
        "state_id": int(row.state_id),
        "body_text": str(row.body_text or ""),
        "interaction_mode": str(payload_obj.get("interaction_mode") or "").strip() or None,
        "active_thread_id": active_thread_id,
        "focus_summary": focus_summary,
        "next_candidate_action": next_candidate_action,
        "talk_candidate": talk_candidate,
        "updated_reason": updated_reason,
        "attention_targets": list(attention_targets),
        "attention_targets_total": int(total_targets_count),
        "last_confirmed_at": _fmt_ts_or_none(int(row.last_confirmed_at) if row.last_confirmed_at is not None else None),
        "updated_at": _fmt_ts_or_none(int(row.updated_at) if row.updated_at is not None else None),
        "dt_seconds": int(dt_seconds),
        "updated_from": updated_from,
        "updated_from_event_ids": [int(x) for x in list(updated_from_event_ids)],
        "updated_from_event_preview": source_event_preview,
    }


def _build_agenda_threads_snapshot(
    *,
    db,
) -> list[dict[str, Any]]:
    """現在の agenda_threads をデバッグ表示用に整形して返す。"""

    # --- active/open を優先して、今見たい thread を上位へ出す。 ---
    status_rank = {
        "active": 0,
        "open": 1,
        "blocked": 2,
        "stale": 3,
        "satisfied": 4,
        "closed": 5,
    }

    rows = db.query(AgendaThread).order_by(AgendaThread.updated_at.desc()).limit(32).all()
    ordered_rows = sorted(
        list(rows or []),
        key=lambda row: (
            int(status_rank.get(str(row.status or ""), 9)),
            -int(row.priority or 0),
            -int(row.updated_at or 0),
            str(row.thread_id or ""),
        ),
    )[: int(_AGENDA_THREADS_LIMIT)]

    out: list[dict[str, Any]] = []
    for row in list(ordered_rows or []):
        next_action_payload = common_utils.json_loads_maybe(str(row.next_action_payload_json or "{}"))
        if not isinstance(next_action_payload, dict):
            next_action_payload = {}
        metadata_obj = common_utils.json_loads_maybe(str(row.metadata_json or "{}"))
        if not isinstance(metadata_obj, dict):
            metadata_obj = {}
        out.append(
            {
                "thread_id": str(row.thread_id),
                "thread_key": str(row.thread_key),
                "kind": str(row.kind),
                "topic": str(row.topic),
                "goal": str(row.goal or "").strip() or None,
                "status": str(row.status),
                "priority": int(row.priority),
                "next_action_type": str(row.next_action_type or "").strip() or None,
                "next_action_payload": dict(next_action_payload),
                "followup_due_at": _fmt_ts_or_none(int(row.followup_due_at)) if row.followup_due_at is not None else None,
                "last_progress_at": _fmt_ts_or_none(int(row.last_progress_at)) if row.last_progress_at is not None else None,
                "last_result_status": str(row.last_result_status or "").strip() or None,
                "report_candidate_level": str(row.report_candidate_level),
                "delivery_mode": resolve_delivery_mode_from_report_candidate_level(str(row.report_candidate_level)),
                "report_candidate_reason": str(row.report_candidate_reason or "").strip() or None,
                "updated_at": _fmt_ts_or_none(int(row.updated_at)),
                "source_event_id": (int(row.source_event_id) if row.source_event_id is not None else None),
                "source_result_id": str(row.source_result_id or "").strip() or None,
                "metadata": dict(metadata_obj),
            }
        )
    return out


def _build_delivery_decision_snapshot(
    *,
    current_thought_snapshot: dict[str, Any] | None,
    agenda_threads_snapshot: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """
    現在の完了時 delivery_decision をデバッグ表示用に整形して返す。

    方針:
        - active thread の report candidate を最優先で表示する。
        - active thread が無ければ、共有候補を持つ先頭 thread を表示する。
        - delivery mode は report_candidate_level から導出する即時発話可否を示す。
    """

    # --- active thread を起点に見る ---
    active_thread_id = None
    if isinstance(current_thought_snapshot, dict):
        active_thread_id = str(current_thought_snapshot.get("active_thread_id") or "").strip() or None

    active_row: dict[str, Any] | None = None
    selected_row: dict[str, Any] | None = None
    if active_thread_id:
        for row in list(agenda_threads_snapshot or []):
            if str(row.get("thread_id") or "") == str(active_thread_id):
                active_row = dict(row)
                break

    # --- active thread 自体に共有候補がある時だけ、それを最優先で表示する ---
    if isinstance(active_row, dict):
        active_report_level = str(active_row.get("report_candidate_level") or "none")
        if active_report_level != "none":
            selected_row = dict(active_row)

    # --- active に共有候補が無ければ、共有候補を持つ先頭を採用する ---
    if selected_row is None:
        for row in list(agenda_threads_snapshot or []):
            if str(row.get("report_candidate_level") or "") != "none":
                selected_row = dict(row)
                break

    # --- 表示対象が無ければ、current_thought 未作成時のみ null を返す ---
    if selected_row is None:
        if current_thought_snapshot is None:
            return None
        return {
            "thread_id": active_thread_id,
            "topic": (
                str(active_row.get("topic") or "").strip() or None
                if isinstance(active_row, dict)
                else None
            ),
            "report_candidate_level": "none",
            "delivery_mode": "silent",
            "reason": None,
        }

    # --- 選ばれた thread から表示構造を作る ---
    return {
        "thread_id": str(selected_row.get("thread_id") or "").strip() or None,
        "topic": str(selected_row.get("topic") or "").strip() or None,
        "report_candidate_level": str(selected_row.get("report_candidate_level") or "none"),
        "delivery_mode": str(selected_row.get("delivery_mode") or "silent"),
        "reason": str(selected_row.get("report_candidate_reason") or "").strip() or None,
    }


@router.get("/debug")
def get_mood_debug(
    config_store: ConfigStore = Depends(get_config_store_dep),
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> dict[str, Any]:
    """
    現在の「背景の気分（LongMoodState）」を、デバッグ観測向けに返す。

    仕様:
        - 認証: Bearer のみ（ルータ登録側で強制）
        - embedding_preset_id: サーバのアクティブ設定を使用
        - long_mood_state が無い場合: 200 + { "mood": null, "recent_affects": [...], "limits": {...} }
        - memory_enabled=false の場合: 503
        - shock_vad: now 時点で時間減衰した値を返す（dt_seconds を併記）
        - recent_affects: 直近の event_affects（全source）を返す（events.searchable=1 のみ）
    """

    # --- 記憶機能が無効なら 503 ---
    if not config_store.memory_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memory is disabled (memory_enabled=false)",
        )

    # --- 現在時刻（UTCのUNIX秒） ---
    now_ts = int(clock_service.now_domain_utc_ts())

    # --- 設定（アクティブDB） ---
    cfg = config_store.config
    embedding_preset_id = str(cfg.embedding_preset_id).strip()
    embedding_dimension = int(cfg.embedding_dimension)

    # --- 記憶DBを開く ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 直近の瞬間感情（event_affects）を取る ---
        # NOTE:
        # - ここは「デバッグ表示」なので、本文の可読性と追跡可能性を優先する。
        # - events.searchable=0（誤想起の分離等）は混ぜない（ノイズを避ける）。
        recent_affects: list[dict[str, Any]] = []
        rows = (
            db.query(
                EventAffect.id,
                EventAffect.event_id,
                Event.source,
                Event.created_at,
                EventAffect.created_at,
                EventAffect.moment_affect_text,
                EventAffect.moment_affect_labels_json,
                EventAffect.vad_v,
                EventAffect.vad_a,
                EventAffect.vad_d,
                EventAffect.confidence,
            )
            .join(Event, Event.event_id == EventAffect.event_id)
            .filter(Event.searchable == 1)
            .order_by(EventAffect.created_at.desc(), EventAffect.id.desc())
            .limit(int(_RECENT_EVENT_AFFECTS_LIMIT))
            .all()
        )
        for r in rows:
            if not r:
                continue
            recent_affects.append(
                {
                    "affect_id": int(r[0] or 0),
                    "event_id": int(r[1] or 0),
                    "event_source": str(r[2] or "").strip(),
                    "event_created_at": format_iso8601_local_with_tz(int(r[3] or 0)),
                    "affect_created_at": format_iso8601_local_with_tz(int(r[4] or 0)),
                    "moment_affect_text": str(r[5] or ""),
                    "moment_affect_labels": common_utils.parse_json_str_list(str(r[6] or "")),
                    "vad": affect.vad_dict(float(r[7] or 0.0), float(r[8] or 0.0), float(r[9] or 0.0)),
                    "confidence": float(r[10] or 0.0),
                }
            )

        # --- バックグラウンド状態（自発行動/worker/runner）を統合デバッグ情報として収集 ---
        background_debug = _build_background_debug_snapshot(
            db=db,
            config_store=config_store,
            now_domain_ts=int(now_ts),
        )
        # --- 現在の思考と agenda を取得 ---
        current_thought_debug = _build_current_thought_snapshot(
            db=db,
            now_domain_ts=int(now_ts),
        )
        agenda_threads_debug = _build_agenda_threads_snapshot(
            db=db,
        )
        delivery_decision_debug = _build_delivery_decision_snapshot(
            current_thought_snapshot=current_thought_debug,
            agenda_threads_snapshot=list(agenda_threads_debug),
        )

        st = (
            db.query(State)
            .filter(State.kind == "long_mood_state")
            .filter(State.searchable == 1)
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .first()
        )

        # --- 未作成なら null ---
        if st is None:
            return {
                "mood": None,
                "current_thought": current_thought_debug,
                "agenda_threads": list(agenda_threads_debug),
                "delivery_decision": delivery_decision_debug,
                "recent_affects": list(recent_affects),
                "background": dict(background_debug),
                "limits": {
                    "recent_affects_limit": int(_RECENT_EVENT_AFFECTS_LIMIT),
                    "recent_intents_limit": int(_RECENT_INTENTS_LIMIT),
                    "recent_agent_jobs_limit": int(_RECENT_AGENT_JOBS_LIMIT),
                    "runtime_attention_targets_limit": int(_RUNTIME_ATTENTION_TARGETS_LIMIT),
                    "current_thought_targets_limit": int(_CURRENT_THOUGHT_TARGETS_LIMIT),
                    "agenda_threads_limit": int(_AGENDA_THREADS_LIMIT),
                },
            }

        # --- payload_json を dict として扱う（壊れていても落とさない） ---
        payload_obj: Any = affect.parse_long_mood_payload(str(st.payload_json or ""))

        # --- payload から VAD を読む（無ければ 0.0 埋め） ---
        baseline_vad = affect.vad_dict(0.0, 0.0, 0.0)
        shock_vad = affect.vad_dict(0.0, 0.0, 0.0)
        if isinstance(payload_obj, dict):
            bv = affect.extract_vad_from_payload_obj(payload_obj, "baseline_vad")
            sv = affect.extract_vad_from_payload_obj(payload_obj, "shock_vad")
            if bv is not None:
                baseline_vad = dict(bv)
            if sv is not None:
                shock_vad = dict(sv)

        # --- dt_seconds（shock減衰に使用） ---
        try:
            dt_seconds = int(now_ts) - int(st.last_confirmed_at)
        except Exception:  # noqa: BLE001
            dt_seconds = 0
        if dt_seconds < 0:
            dt_seconds = 0

        # --- shock 半減期（payloadが無い/壊れている場合は既定） ---
        shock_halflife_seconds = 0
        if isinstance(payload_obj, dict):
            try:
                shock_halflife_seconds = int(payload_obj.get("shock_halflife_seconds") or 0)
            except Exception:  # noqa: BLE001
                shock_halflife_seconds = 0

        # --- shock を now 時点で減衰 ---
        shock_decayed = affect.decay_shock_for_snapshot(
            shock_vad=shock_vad,
            dt_seconds=int(dt_seconds),
            shock_halflife_seconds=int(shock_halflife_seconds),
        )

        # --- baseline + shock（減衰後） ---
        combined_vad = affect.vad_add(baseline_vad, shock_decayed)

        return {
            "mood": {
                "state_id": int(st.state_id),
                "body_text": str(st.body_text),
                "confidence": float(st.confidence),
                "payload": payload_obj,
                "baseline_vad": baseline_vad,
                "shock_vad": shock_decayed,
                "vad": combined_vad,
                "now": format_iso8601_local_with_tz(int(now_ts)),
                "dt_seconds": int(dt_seconds),
                "last_confirmed_at": format_iso8601_local_with_tz(int(st.last_confirmed_at)),
            },
            "current_thought": current_thought_debug,
            "agenda_threads": list(agenda_threads_debug),
            "delivery_decision": delivery_decision_debug,
            "recent_affects": list(recent_affects),
            "background": dict(background_debug),
            "limits": {
                "recent_affects_limit": int(_RECENT_EVENT_AFFECTS_LIMIT),
                "recent_intents_limit": int(_RECENT_INTENTS_LIMIT),
                "recent_agent_jobs_limit": int(_RECENT_AGENT_JOBS_LIMIT),
                "runtime_attention_targets_limit": int(_RUNTIME_ATTENTION_TARGETS_LIMIT),
                "current_thought_targets_limit": int(_CURRENT_THOUGHT_TARGETS_LIMIT),
                "agenda_threads_limit": int(_AGENDA_THREADS_LIMIT),
            },
        }
