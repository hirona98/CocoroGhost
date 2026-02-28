"""
control API サービス。

目的:
    - `/api/control*` の業務ロジックを router から分離する。
    - process/time/autonomy/agent-job の制御を機能単位でまとめる。
"""

from __future__ import annotations

import logging
import os
import signal
import time

from fastapi import BackgroundTasks, HTTPException, Response, status

from cocoro_ghost import schemas, worker
from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.autonomy.repository import AutonomyRepository
from cocoro_ghost.clock import ClockService
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import load_global_settings, settings_session_scope
from cocoro_ghost.runtime import event_stream, log_stream
from cocoro_ghost.time_utils import format_iso8601_local_with_tz
from cocoro_ghost.worker_handlers_autonomy import complete_agent_job_from_runner, fail_agent_job_from_runner


logger = logging.getLogger(__name__)


def _get_autonomy_repo_from_active_config() -> AutonomyRepository:
    """
    現在の memory DB に対応する AutonomyRepository を返す。
    """

    # --- アクティブな embedding 設定で repository を固定する ---
    cfg = get_config_store().config
    return AutonomyRepository(
        embedding_preset_id=str(cfg.embedding_preset_id),
        embedding_dimension=int(cfg.embedding_dimension),
    )


def _publish_agent_job_activity(
    *,
    state: str,
    job_id: str,
    intent_id: str | None,
    decision_id: str | None,
    backend: str | None,
    task_instruction: str | None,
) -> None:
    """
    agent_job の監視イベントを events/stream へ配信する。
    """

    # --- claim は DB 保存を伴わないため event_id=0 の監視イベントにする ---
    event_stream.publish(
        type="autonomy.activity",
        event_id=0,
        data={
            "phase": "agent_job",
            "state": str(state or ""),
            "action_type": "agent_delegate",
            "capability": "agent_delegate",
            "backend": (str(backend) if backend else None),
            "result_status": None,
            "summary_text": (str(task_instruction) if task_instruction else None),
            "decision_id": (str(decision_id) if decision_id else None),
            "intent_id": (str(intent_id) if intent_id else None),
            "result_id": None,
            "agent_job_id": (str(job_id) if job_id else None),
            "goal_id": None,
        },
        target_client_id=None,
    )


def _request_process_shutdown(*, reason: str | None) -> None:
    """
    現在プロセスへ SIGTERM を送り、終了を要求する。
    """

    # --- 操作ログを残した上で、レスポンス返却猶予のため短く待つ ---
    logger.warning("shutdown requested", extra={"reason": (reason or "").strip()})
    time.sleep(0.2)
    os.kill(os.getpid(), signal.SIGTERM)


def _build_time_snapshot_response(clock_service: ClockService) -> schemas.ControlTimeSnapshotResponse:
    """
    ClockService の現在値を API スキーマへ整形する。
    """

    # --- system/domain 両方を API 向けに整形する ---
    snap = clock_service.snapshot()
    return schemas.ControlTimeSnapshotResponse(
        system_now_utc_ts=int(snap.system_now_utc_ts),
        system_now_iso=format_iso8601_local_with_tz(int(snap.system_now_utc_ts)),
        domain_now_utc_ts=int(snap.domain_now_utc_ts),
        domain_now_iso=format_iso8601_local_with_tz(int(snap.domain_now_utc_ts)),
        domain_offset_seconds=int(snap.domain_offset_seconds),
    )


def schedule_process_control(
    *,
    request: schemas.ControlRequest,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    プロセス制御コマンドを受理する。
    """

    # --- action バリデーションは schemas 側で完了している ---
    background_tasks.add_task(_request_process_shutdown, reason=request.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT, background=background_tasks)


def build_stream_stats_response() -> schemas.StreamRuntimeStatsResponse:
    """
    event/log stream の統計を返す。
    """

    # --- 各ストリームの統計スナップショットをまとめる ---
    event_stats = event_stream.get_runtime_stats()
    log_stats = log_stream.get_runtime_stats()
    return schemas.StreamRuntimeStatsResponse(
        events=schemas.EventStreamRuntimeStats(**event_stats),
        logs=schemas.LogStreamRuntimeStats(**log_stats),
    )


def build_worker_stats_response() -> schemas.WorkerRuntimeStatsResponse:
    """
    worker のジョブキュー統計を返す。
    """

    # --- アクティブな memory DB に対するジョブ統計を返す ---
    cfg = get_config_store().config
    stats = worker.get_job_queue_stats(
        embedding_preset_id=str(cfg.embedding_preset_id),
        embedding_dimension=int(cfg.embedding_dimension),
    )
    return schemas.WorkerRuntimeStatsResponse(**stats)


def get_time_snapshot(*, clock_service: ClockService) -> schemas.ControlTimeSnapshotResponse:
    """
    現在の時刻スナップショットを返す。
    """

    # --- 現在値をそのまま返す ---
    return _build_time_snapshot_response(clock_service)


def advance_time(
    *,
    request: schemas.ControlTimeAdvanceRequest,
    clock_service: ClockService,
) -> schemas.ControlTimeSnapshotResponse:
    """
    domain 時刻を前進させる。
    """

    # --- 不正な秒数は 400 にする ---
    try:
        clock_service.advance_domain_seconds(seconds=int(request.seconds))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _build_time_snapshot_response(clock_service)


def reset_time(*, clock_service: ClockService) -> schemas.ControlTimeSnapshotResponse:
    """
    domain 時刻オフセットを 0 に戻す。
    """

    # --- system 時刻は触らずに domain offset だけ戻す ---
    clock_service.reset_domain_offset()
    return _build_time_snapshot_response(clock_service)


def get_autonomy_status() -> schemas.ControlAutonomyStatusResponse:
    """
    自発行動の稼働状態を返す。
    """

    # --- orchestrator の公開ステータスをそのまま返す ---
    orchestrator = get_autonomy_orchestrator()
    return schemas.ControlAutonomyStatusResponse(**orchestrator.get_status())


def set_autonomy_enabled(*, enabled: bool) -> schemas.ControlAutonomyToggleResponse:
    """
    自発行動の有効状態を切り替える。
    """

    # --- settings DB の単一フラグを切り替える ---
    with settings_session_scope() as db:
        settings = load_global_settings(db)
        settings.autonomy_enabled = bool(enabled)
    return schemas.ControlAutonomyToggleResponse(autonomy_enabled=bool(enabled))


def trigger_autonomy(
    *,
    request: schemas.ControlAutonomyTriggerRequest,
) -> schemas.ControlAutonomyToggleResponse:
    """
    手動トリガを投入する。
    """

    # --- 重複していなければ手動トリガを投入する ---
    orchestrator = get_autonomy_orchestrator()
    inserted = orchestrator.enqueue_manual_trigger(
        trigger_type=str(request.trigger_type),
        trigger_key=str(request.trigger_key),
        payload=dict(request.payload or {}),
        scheduled_at=(int(request.scheduled_at) if request.scheduled_at is not None else None),
    )
    if not bool(inserted):
        raise HTTPException(status_code=409, detail="trigger already queued or claimed")

    # --- 現在の有効状態を返す ---
    with settings_session_scope() as db:
        settings = load_global_settings(db)
        enabled = bool(getattr(settings, "autonomy_enabled", False))
    return schemas.ControlAutonomyToggleResponse(autonomy_enabled=bool(enabled))


def list_autonomy_intents(*, limit: int) -> schemas.ControlAutonomyIntentsResponse:
    """
    intent 一覧を返す。
    """

    # --- orchestrator が持つ一覧 API をスキーマへ詰め替える ---
    orchestrator = get_autonomy_orchestrator()
    items = orchestrator.list_intents(limit=int(limit))
    return schemas.ControlAutonomyIntentsResponse(
        items=[schemas.ControlAutonomyIntentItem(**item) for item in items]
    )


def claim_agent_jobs(
    *,
    request: schemas.ControlAgentJobClaimRequest,
    clock_service: ClockService,
) -> schemas.ControlAgentJobClaimResponse:
    """
    queued な agent_job を claim して返す。
    """

    # --- 現在時刻と repo を決めて claim する ---
    repo = _get_autonomy_repo_from_active_config()
    now_system_ts = int(clock_service.now_system_utc_ts())
    items = repo.claim_agent_jobs(
        runner_id=str(request.runner_id),
        backends=[str(x) for x in list(request.backends or [])],
        now_system_ts=int(now_system_ts),
        limit=int(request.limit),
    )

    # --- claimed は監視イベントへ流す ---
    for item in list(items):
        _publish_agent_job_activity(
            state="claimed",
            job_id=str(item.get("job_id") or ""),
            intent_id=(str(item.get("intent_id")) if item.get("intent_id") else None),
            decision_id=(str(item.get("decision_id")) if item.get("decision_id") else None),
            backend=(str(item.get("backend")) if item.get("backend") else None),
            task_instruction=(str(item.get("task_instruction")) if item.get("task_instruction") else None),
        )
    return schemas.ControlAgentJobClaimResponse(
        items=[schemas.ControlAgentJobClaimItem(**item) for item in items]
    )


def heartbeat_agent_job(
    *,
    job_id: str,
    request: schemas.ControlAgentJobHeartbeatRequest,
    clock_service: ClockService,
) -> schemas.ControlAgentJobMutationResponse:
    """
    実行中 agent_job の heartbeat を更新する。
    """

    # --- claim token / runner_id が一致する場合だけ更新する ---
    repo = _get_autonomy_repo_from_active_config()
    now_system_ts = int(clock_service.now_system_utc_ts())
    ok = repo.heartbeat_agent_job(
        job_id=str(job_id),
        claim_token=str(request.claim_token),
        runner_id=str(request.runner_id),
        now_system_ts=int(now_system_ts),
    )
    if not bool(ok):
        raise HTTPException(status_code=409, detail="agent job heartbeat rejected")

    # --- 更新後状態を返す ---
    item = repo.get_agent_job(job_id=str(job_id))
    if item is None:
        raise HTTPException(status_code=404, detail="agent job not found")
    return schemas.ControlAgentJobMutationResponse(
        ok=True,
        job_id=str(job_id),
        status=str(item["status"]),
    )


def complete_agent_job(
    *,
    job_id: str,
    request: schemas.ControlAgentJobCompleteRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    agent_runner の complete callback を処理する。
    """

    # --- アクティブ memory DB に対して callback を反映する ---
    cfg = get_config_store().config
    try:
        out = complete_agent_job_from_runner(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
            job_id=str(job_id),
            claim_token=str(request.claim_token),
            runner_id=str(request.runner_id),
            result_status=str(request.result_status),
            summary_text=str(request.summary_text),
            details_json_obj=dict(request.details_json or {}),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return schemas.ControlAgentJobMutationResponse(
        ok=True,
        job_id=str(out["job_id"]),
        status=str(out["status"]),
    )


def fail_agent_job(
    *,
    job_id: str,
    request: schemas.ControlAgentJobFailRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    agent_runner の fail callback を処理する。
    """

    # --- アクティブ memory DB に対して failure を反映する ---
    cfg = get_config_store().config
    try:
        out = fail_agent_job_from_runner(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
            job_id=str(job_id),
            claim_token=str(request.claim_token),
            runner_id=str(request.runner_id),
            error_code=(str(request.error_code) if request.error_code is not None else None),
            error_message=str(request.error_message),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return schemas.ControlAgentJobMutationResponse(
        ok=True,
        job_id=str(out["job_id"]),
        status=str(out["status"]),
    )


def list_agent_jobs(
    *,
    limit: int,
    job_status: str | None,
    backend: str | None,
) -> schemas.ControlAgentJobsResponse:
    """
    agent_jobs 一覧を返す。
    """

    # --- 監視 UI 向けに新しい順の一覧を返す ---
    repo = _get_autonomy_repo_from_active_config()
    items = repo.list_recent_agent_jobs(
        limit=int(limit),
        status=job_status,
        backend=backend,
    )
    return schemas.ControlAgentJobsResponse(
        items=[schemas.ControlAgentJobItem(**item) for item in items]
    )


def get_agent_job(*, job_id: str) -> schemas.ControlAgentJobResponse:
    """
    agent_job 1 件を返す。
    """

    # --- 1 件だけ取得し、なければ 404 にする ---
    repo = _get_autonomy_repo_from_active_config()
    item = repo.get_agent_job(job_id=str(job_id))
    if item is None:
        raise HTTPException(status_code=404, detail="agent job not found")
    return schemas.ControlAgentJobResponse(item=schemas.ControlAgentJobItem(**item))
