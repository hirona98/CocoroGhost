"""
schedule_alarm capability。

役割:
    - `schedule_action` を reminders.db の one-shot reminder として登録する。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost.core import common_utils
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult
from cocoro_ghost.core.clock import get_clock_service
from cocoro_ghost.reminders.db import reminders_session_scope
from cocoro_ghost.reminders.models import Reminder


def execute_schedule_alarm(*, action_payload: dict[str, Any]) -> CapabilityExecutionResult:
    """
    schedule_action を実行する（one-shot reminder 登録）。
    """

    # --- 入力を正規化 ---
    payload = dict(action_payload or {})
    at_raw = payload.get("at")
    at_utc = int(at_raw) if isinstance(at_raw, (int, float)) else 0
    if at_utc <= 0:
        return CapabilityExecutionResult(
            result_status="failed",
            summary="schedule_action には `at`（UTC UNIX秒）が必要です。",
            result_payload_json=common_utils.json_dumps({"action_payload": payload}),
            useful_for_recall_hint=0,
            next_trigger=None,
        )

    # --- リマインダー本文を決める ---
    content = str(payload.get("content") or "").strip()
    if not content:
        action_obj = payload.get("action")
        if isinstance(action_obj, dict):
            content = common_utils.json_dumps(action_obj)
        else:
            content = str(action_obj or "").strip()
    if not content:
        return CapabilityExecutionResult(
            result_status="failed",
            summary="schedule_action には `content` または `action` が必要です。",
            result_payload_json=common_utils.json_dumps({"action_payload": payload}),
            useful_for_recall_hint=0,
            next_trigger=None,
        )

    # --- 過去時刻の登録は明示的に拒否する ---
    now_domain_ts = int(get_clock_service().now_domain_utc_ts())
    if int(at_utc) <= int(now_domain_ts):
        return CapabilityExecutionResult(
            result_status="failed",
            summary="schedule_action の `at` は現在より未来である必要があります。",
            result_payload_json=common_utils.json_dumps(
                {"action_payload": payload, "now_domain_utc_ts": int(now_domain_ts)}
            ),
            useful_for_recall_hint=0,
            next_trigger=None,
        )

    # --- reminders.db に one-shot reminder を登録 ---
    with reminders_session_scope() as db:
        row = Reminder(
            enabled=True,
            repeat_kind="once",
            scheduled_at_utc=int(at_utc),
            time_of_day=None,
            weekdays_mask=None,
            content=str(content)[:4000],
            next_fire_at_utc=int(at_utc),
            last_fired_at_utc=None,
        )
        db.add(row)
        db.flush()
        reminder_id = str(row.id)

    # --- 実行結果を返す ---
    return CapabilityExecutionResult(
        result_status="success",
        summary=f"リマインダーを登録しました（at={int(at_utc)}）。",
        result_payload_json=common_utils.json_dumps(
            {
                "reminder_id": str(reminder_id),
                "scheduled_at_utc": int(at_utc),
                "content": str(content),
            }
        ),
        useful_for_recall_hint=1,
        next_trigger=None,
    )
