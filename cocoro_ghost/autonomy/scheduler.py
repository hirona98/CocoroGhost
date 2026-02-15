"""
自律ループジョブの投入ヘルパ。

役割:
- `run_autonomy_cycle` の enqueue と重複抑止を提供する。
"""

from __future__ import annotations

import time

from sqlalchemy import func

from cocoro_ghost import common_utils
from cocoro_ghost.autonomy import runtime_control
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import Job
from cocoro_ghost.worker_constants import JOB_PENDING, JOB_RUNNING


def _now_utc_ts() -> int:
    """現在UTC時刻をUNIX秒で返す。"""

    return int(time.time())


def enqueue_autonomy_cycle_job(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    trigger_type: str,
    event_id: int | None = None,
    result_id: str | None = None,
) -> bool:
    """自律ループジョブを投入する。重複時は投入しない。"""

    # --- 無効時は投入しない ---
    if not runtime_control.is_enabled():
        return False

    # --- 入力正規化 ---
    trigger_s = str(trigger_type or "").strip() or "periodic"
    result_id_s = str(result_id or "").strip()
    now_ts = _now_utc_ts()

    # --- action_result は result_id を必須とする ---
    if trigger_s == "action_result" and not result_id_s:
        raise RuntimeError("result_id is required for trigger_type=action_result")

    # --- pending/running があれば投入しない ---
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        # --- action_result 同一result_id の多重投入を禁止する ---
        if trigger_s == "action_result":
            existing_same_result = (
                db.query(func.count(Job.id))
                .filter(Job.kind == "run_autonomy_cycle")
                .filter(func.json_extract(Job.payload_json, "$.trigger_type") == "action_result")
                .filter(func.json_extract(Job.payload_json, "$.result_id") == result_id_s)
                .scalar()
            )
            if int(existing_same_result or 0) > 0:
                return False

        running_or_pending = (
            db.query(func.count(Job.id))
            .filter(Job.kind == "run_autonomy_cycle")
            .filter(Job.status.in_([int(JOB_PENDING), int(JOB_RUNNING)]))
            .scalar()
        )
        if int(running_or_pending or 0) > 0:
            return False

        payload: dict[str, object] = {
            "trigger_type": trigger_s,
            "requested_at": int(now_ts),
        }
        if event_id is not None and int(event_id) > 0:
            payload["event_id"] = int(event_id)
        if trigger_s == "action_result":
            payload["result_id"] = result_id_s

        # --- ジョブを追加 ---
        db.add(
            Job(
                kind="run_autonomy_cycle",
                payload_json=common_utils.json_dumps(payload),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )
    return True
