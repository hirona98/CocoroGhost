"""
内部基本 capability `speak` の adapter。

役割:
- 自発発話を events（source=meta_proactive）へ永続化する。
- events/stream へ通知し、WritePlan 連携ジョブを投入する。
"""

from __future__ import annotations

import time
from typing import Any

from cocoro_ghost import common_utils, event_stream
from cocoro_ghost.autonomy.capability_adapters.base import (
    AdapterExecutionContext,
    AdapterExecutionOutput,
    CapabilityAdapter,
)
from cocoro_ghost.autonomy.scheduler import enqueue_autonomy_cycle_job
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import Event, Job
from cocoro_ghost.worker_constants import JOB_PENDING


def _now_utc_ts() -> int:
    """現在UTC時刻をUNIX秒で返す。"""

    return int(time.time())


def _enqueue_write_plan_chain(*, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
    """result event 向けの非同期ジョブチェーンを投入する。"""

    # --- 投入時刻を固定 ---
    now_ts = _now_utc_ts()

    # --- 既存経路と同じ3段チェーンを投入 ---
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        db.add(
            Job(
                kind="upsert_event_embedding",
                payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )
        db.add(
            Job(
                kind="upsert_event_assistant_summary",
                payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )
        db.add(
            Job(
                kind="generate_write_plan",
                payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )


class SpeakCapabilityAdapter(CapabilityAdapter):
    """`speak` capability adapter 実装。"""

    @property
    def capability_id(self) -> str:
        """担当 capability_id を返す。"""

        return "speak"

    def execute(
        self,
        *,
        context: AdapterExecutionContext,
        input_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> AdapterExecutionOutput:
        """`speak.emit` を1回実行する。"""

        # --- 未使用引数を明示 ---
        _ = timeout_seconds

        # --- operation を検証 ---
        if str(context.operation) != "emit":
            raise RuntimeError(f"unsupported operation: {context.operation}")

        # --- 入力を正規化 ---
        message = str(input_payload.get("message") or "").strip()
        if not message:
            raise RuntimeError("input_payload.message is required")
        target_client_id_raw = input_payload.get("target_client_id")
        target_client_id = str(target_client_id_raw).strip() if target_client_id_raw is not None else ""
        now_ts = _now_utc_ts()

        # --- result event を永続化 ---
        with memory_session_scope(str(context.embedding_preset_id), int(context.embedding_dimension)) as db:
            ev = Event(
                created_at=int(now_ts),
                updated_at=int(now_ts),
                client_id=(target_client_id if target_client_id else None),
                source="meta_proactive",
                user_text=None,
                assistant_text=str(message),
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- クライアントへ配信 ---
        event_stream.publish(
            type="meta-request",
            event_id=int(event_id),
            data={"message": str(message)},
            target_client_id=(target_client_id if target_client_id else None),
        )

        # --- WritePlan 連携ジョブを投入 ---
        _enqueue_write_plan_chain(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            event_id=int(event_id),
        )

        # --- event 起点の自律ループも投入（重複抑止は scheduler 側） ---
        enqueue_autonomy_cycle_job(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            trigger_type="event_created",
            event_id=int(event_id),
        )

        # --- adapter 実行結果を返す ---
        return AdapterExecutionOutput(
            result_payload={
                "event_id": int(event_id),
                "source": "meta_proactive",
                "message": str(message),
            },
            effects=[
                {"effect_type": "event.persisted", "event_id": int(event_id), "source": "meta_proactive"},
                {"effect_type": "event_stream.published", "event_id": int(event_id), "stream_type": "meta-request"},
            ],
            meta_json={
                "target_client_id": (target_client_id if target_client_id else None),
            },
        )
