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

from cocoro_ghost import affect
from cocoro_ghost import worker
from cocoro_ghost.clock import ClockService
from cocoro_ghost import common_utils
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.deps import get_clock_service_dep, get_config_store_dep
from cocoro_ghost.autonomy.runtime_blackboard import get_runtime_blackboard
from cocoro_ghost.memory_models import AgentJob, AutonomyTrigger, Event, EventAffect, Intent, State
from cocoro_ghost.time_utils import format_iso8601_local_with_tz
from cocoro_ghost.worker_constants import AGENT_JOB_STALE_SECONDS as _AGENT_JOB_STALE_SECONDS


router = APIRouter(prefix="/mood", tags=["mood"])

# --- デバッグ用: 直近event_affectの件数（固定） ---
# NOTE:
# - クエリパラメータで増減させると互換や運用が複雑になるため、サーバ側で固定する。
# - まずは「直近の揺れ」を見る用途なので、少数（例: 8）で十分。
_RECENT_EVENT_AFFECTS_LIMIT = 8
_RECENT_INTENTS_LIMIT = 8
_RECENT_AGENT_JOBS_LIMIT = 8
_RUNTIME_ATTENTION_TARGETS_LIMIT = 8


def _fmt_ts_or_none(ts: int | None) -> str | None:
    """UNIX秒をローカルISO文字列へ変換する（NoneはNone）。"""

    # --- null はそのまま返す ---
    if ts is None:
        return None

    # --- フォーマット失敗は例外化してデバッグAPIの不整合を表面化する ---
    return format_iso8601_local_with_tz(int(ts))


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

    # --- worker jobs の統計（memory jobs テーブル） ---
    worker_stats = worker.get_job_queue_stats(
        embedding_preset_id=str(embedding_preset_id),
        embedding_dimension=int(embedding_dimension),
    )

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

    # --- agent_jobs の滞留数 ---
    now_system_ts = int(time.time())
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
        recent_intents.append(
            {
                "intent_id": str(row.intent_id),
                "action_type": str(row.action_type),
                "status": str(row.status),
                "priority": int(row.priority or 0),
                "goal_id": (str(row.goal_id) if row.goal_id is not None else None),
                "scheduled_at": _fmt_ts_or_none(int(row.scheduled_at) if row.scheduled_at is not None else None),
                "updated_at": _fmt_ts_or_none(int(row.updated_at)),
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
            "enabled": bool(getattr(cfg, "autonomy_enabled", False)),
            "heartbeat_seconds": int(getattr(cfg, "autonomy_heartbeat_seconds", 30)),
            "max_parallel_intents": int(getattr(cfg, "autonomy_max_parallel_intents", 2)),
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
                "recent_affects": list(recent_affects),
                "background": dict(background_debug),
                "limits": {
                    "recent_affects_limit": int(_RECENT_EVENT_AFFECTS_LIMIT),
                    "recent_intents_limit": int(_RECENT_INTENTS_LIMIT),
                    "recent_agent_jobs_limit": int(_RECENT_AGENT_JOBS_LIMIT),
                    "runtime_attention_targets_limit": int(_RUNTIME_ATTENTION_TARGETS_LIMIT),
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
            "recent_affects": list(recent_affects),
            "background": dict(background_debug),
            "limits": {
                "recent_affects_limit": int(_RECENT_EVENT_AFFECTS_LIMIT),
                "recent_intents_limit": int(_RECENT_INTENTS_LIMIT),
                "recent_agent_jobs_limit": int(_RECENT_AGENT_JOBS_LIMIT),
                "runtime_attention_targets_limit": int(_RUNTIME_ATTENTION_TARGETS_LIMIT),
            },
        }
