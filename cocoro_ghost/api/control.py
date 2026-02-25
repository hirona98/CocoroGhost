"""
/control エンドポイント

プロセス自体の制御（終了要求など）を受け付ける。
本APIは CocoroConsole 等の管理UIから呼び出される想定で、Bearer 認証必須とする。
"""

from __future__ import annotations

import os
import signal
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status

from cocoro_ghost.autonomy.repository import AutonomyRepository
from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.clock import ClockService
from cocoro_ghost import event_stream, log_stream, schemas, worker
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import load_global_settings, settings_session_scope
from cocoro_ghost.deps import get_clock_service_dep
from cocoro_ghost.time_utils import format_iso8601_local_with_tz
from cocoro_ghost.worker_handlers_autonomy import complete_agent_job_from_runner, fail_agent_job_from_runner

logger = __import__("logging").getLogger(__name__)

router = APIRouter()


def _get_autonomy_repo_from_active_config() -> AutonomyRepository:
    """アクティブ memory DB を使う autonomy repository を返す。"""

    # --- 現在の embedding 設定を取得 ---
    cfg = get_config_store().config
    return AutonomyRepository(
        embedding_preset_id=str(cfg.embedding_preset_id),
        embedding_dimension=int(cfg.embedding_dimension),
    )


def _publish_agent_job_activity_from_control(
    *,
    state: str,
    job_id: str,
    intent_id: str | None,
    decision_id: str | None,
    backend: str | None,
    task_instruction: str | None,
) -> None:
    """
    control API で観測できる agent_job 状態を autonomy.activity として配信する。

    方針:
        - claim は DB保存を伴わない WebSocket監視イベントなので event_id=0 を使う。
        - payload は監視UI向けの構造に限定し、本文の意味判定はしない。
    """
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


def _request_process_shutdown(*, reason: Optional[str]) -> None:
    """
    CocoroGhost プロセスの終了を要求する。

    FastAPI のハンドラ内で即座に終了させると、HTTP レスポンスの返却が不安定になるため、
    BackgroundTask として少し遅延させてから SIGTERM を送る。
    """

    # --- ログ出力（UI 操作の追跡用） ---
    logger.warning("shutdown requested", extra={"reason": (reason or "").strip()})

    # --- 先にレスポンスを返しやすくするための短い遅延 ---
    time.sleep(0.2)

    # --- uvicorn に停止シグナルを送る（shutdown イベントが走る） ---
    os.kill(os.getpid(), signal.SIGTERM)


def _build_time_snapshot_response(clock_service: ClockService) -> schemas.ControlTimeSnapshotResponse:
    """現在のsystem/domain時刻をAPIレスポンスへ整形する。"""

    # --- スナップショットを取得 ---
    snap = clock_service.snapshot()

    # --- ISO表示（ローカルTZ付き）を付ける ---
    return schemas.ControlTimeSnapshotResponse(
        system_now_utc_ts=int(snap.system_now_utc_ts),
        system_now_iso=format_iso8601_local_with_tz(int(snap.system_now_utc_ts)),
        domain_now_utc_ts=int(snap.domain_now_utc_ts),
        domain_now_iso=format_iso8601_local_with_tz(int(snap.domain_now_utc_ts)),
        domain_offset_seconds=int(snap.domain_offset_seconds),
    )


@router.post("/control", status_code=status.HTTP_204_NO_CONTENT)
def control(
    request: schemas.ControlRequest,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    プロセス制御コマンドを受け付ける。

    現状は shutdown のみをサポートし、受理後にプロセス終了を要求する。
    """

    # --- action バリデーションは schemas 側で実施済み ---
    background_tasks.add_task(_request_process_shutdown, reason=request.reason)
    # --- BackgroundTasks を紐づける（これが無いと shutdown が実行されない） ---
    return Response(status_code=status.HTTP_204_NO_CONTENT, background=background_tasks)


@router.get("/control/stream-stats", response_model=schemas.StreamRuntimeStatsResponse)
def stream_stats() -> schemas.StreamRuntimeStatsResponse:
    """
    ストリームのランタイム統計を返す。

    運用時に queue逼迫/ドロップ/送信失敗を確認するために使う。
    """

    # --- 各ストリームから統計スナップショットを取得する ---
    event_stats = event_stream.get_runtime_stats()
    log_stats = log_stream.get_runtime_stats()

    # --- スキーマへ詰め替えて返す ---
    return schemas.StreamRuntimeStatsResponse(
        events=schemas.EventStreamRuntimeStats(**event_stats),
        logs=schemas.LogStreamRuntimeStats(**log_stats),
    )


@router.get("/control/worker-stats", response_model=schemas.WorkerRuntimeStatsResponse)
def worker_stats() -> schemas.WorkerRuntimeStatsResponse:
    """
    Workerのジョブキュー統計を返す。

    pending/running/stale の詰まり具合を運用時に確認するために使う。
    """

    # --- アクティブな embedding 設定を取得する ---
    from cocoro_ghost.config import get_config_store

    cfg = get_config_store().config

    # --- jobs テーブル統計を取得する ---
    stats = worker.get_job_queue_stats(
        embedding_preset_id=str(cfg.embedding_preset_id),
        embedding_dimension=int(cfg.embedding_dimension),
    )

    # --- スキーマへ詰め替えて返す ---
    return schemas.WorkerRuntimeStatsResponse(**stats)


@router.get("/control/time", response_model=schemas.ControlTimeSnapshotResponse)
def control_time_snapshot(
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlTimeSnapshotResponse:
    """
    時刻スナップショットを返す。

    - system: OS実時間
    - domain: 会話/記憶/感情の評価に使う論理時刻
    """

    # --- 現在値を返す ---
    return _build_time_snapshot_response(clock_service)


@router.post("/control/time/advance", response_model=schemas.ControlTimeSnapshotResponse)
def control_time_advance(
    request: schemas.ControlTimeAdvanceRequest,
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlTimeSnapshotResponse:
    """
    domain時刻を前進させる。

    注意:
        - system時刻は変更しない。
        - seconds は1以上のみ許可する。
    """

    # --- 入力秒を反映 ---
    try:
        clock_service.advance_domain_seconds(seconds=int(request.seconds))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- 変更後スナップショット ---
    return _build_time_snapshot_response(clock_service)


@router.post("/control/time/reset", response_model=schemas.ControlTimeSnapshotResponse)
def control_time_reset(
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlTimeSnapshotResponse:
    """
    domain時刻オフセットをリセットする。

    system時刻には影響しない。
    """

    # --- offsetを0へ戻す ---
    clock_service.reset_domain_offset()

    # --- 変更後スナップショット ---
    return _build_time_snapshot_response(clock_service)


@router.get("/control/autonomy/status", response_model=schemas.ControlAutonomyStatusResponse)
def control_autonomy_status() -> schemas.ControlAutonomyStatusResponse:
    """
    自発行動の稼働状態と滞留数を返す。
    """

    orchestrator = get_autonomy_orchestrator()
    return schemas.ControlAutonomyStatusResponse(**orchestrator.get_status())


@router.post("/control/autonomy/start", response_model=schemas.ControlAutonomyToggleResponse)
def control_autonomy_start() -> schemas.ControlAutonomyToggleResponse:
    """
    自発行動を有効化する。
    """

    with settings_session_scope() as db:
        settings = load_global_settings(db)
        settings.autonomy_enabled = True
    return schemas.ControlAutonomyToggleResponse(autonomy_enabled=True)


@router.post("/control/autonomy/stop", response_model=schemas.ControlAutonomyToggleResponse)
def control_autonomy_stop() -> schemas.ControlAutonomyToggleResponse:
    """
    自発行動を無効化する。
    """

    with settings_session_scope() as db:
        settings = load_global_settings(db)
        settings.autonomy_enabled = False
    return schemas.ControlAutonomyToggleResponse(autonomy_enabled=False)


@router.post("/control/autonomy/trigger", response_model=schemas.ControlAutonomyToggleResponse)
def control_autonomy_trigger(request: schemas.ControlAutonomyTriggerRequest) -> schemas.ControlAutonomyToggleResponse:
    """
    手動トリガを投入する（デバッグ用）。
    """

    orchestrator = get_autonomy_orchestrator()
    inserted = orchestrator.enqueue_manual_trigger(
        trigger_type=str(request.trigger_type),
        trigger_key=str(request.trigger_key),
        payload=dict(request.payload or {}),
        scheduled_at=(int(request.scheduled_at) if request.scheduled_at is not None else None),
    )
    if not bool(inserted):
        raise HTTPException(status_code=409, detail="trigger already queued or claimed")
    with settings_session_scope() as db:
        settings = load_global_settings(db)
        enabled = bool(getattr(settings, "autonomy_enabled", False))
    return schemas.ControlAutonomyToggleResponse(autonomy_enabled=bool(enabled))


@router.get("/control/autonomy/intents", response_model=schemas.ControlAutonomyIntentsResponse)
def control_autonomy_intents(
    limit: int = Query(default=50, ge=1, le=500),
) -> schemas.ControlAutonomyIntentsResponse:
    """
    intent 一覧を返す。
    """

    orchestrator = get_autonomy_orchestrator()
    items = orchestrator.list_intents(limit=int(limit))
    return schemas.ControlAutonomyIntentsResponse(
        items=[schemas.ControlAutonomyIntentItem(**item) for item in items]
    )


@router.post("/control/agent-jobs/claim", response_model=schemas.ControlAgentJobClaimResponse)
def control_agent_jobs_claim(
    request: schemas.ControlAgentJobClaimRequest,
) -> schemas.ControlAgentJobClaimResponse:
    """
    agent_runner 向けに queued agent_job を claim して返す。
    """

    # --- repo と時刻 ---
    repo = _get_autonomy_repo_from_active_config()
    clock = get_clock_service_dep()
    now_system_ts = int(clock.now_system_utc_ts())

    # --- claim 実行 ---
    items = repo.claim_agent_jobs(
        runner_id=str(request.runner_id),
        backends=[str(x) for x in list(request.backends or [])],
        now_system_ts=int(now_system_ts),
        limit=int(request.limit),
    )
    # --- claimed は監視用に events/stream へ配信する ---
    for item in list(items):
        _publish_agent_job_activity_from_control(
            state="claimed",
            job_id=str(item.get("job_id") or ""),
            intent_id=(str(item.get("intent_id")) if item.get("intent_id") else None),
            decision_id=(str(item.get("decision_id")) if item.get("decision_id") else None),
            backend=(str(item.get("backend")) if item.get("backend") else None),
            task_instruction=(str(item.get("task_instruction")) if item.get("task_instruction") else None),
        )
    return schemas.ControlAgentJobClaimResponse(items=[schemas.ControlAgentJobClaimItem(**x) for x in items])


@router.post("/control/agent-jobs/{job_id}/heartbeat", response_model=schemas.ControlAgentJobMutationResponse)
def control_agent_job_heartbeat(
    job_id: str,
    request: schemas.ControlAgentJobHeartbeatRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    実行中 agent_job の heartbeat を更新する。
    """

    # --- repo と時刻 ---
    repo = _get_autonomy_repo_from_active_config()
    clock = get_clock_service_dep()
    now_system_ts = int(clock.now_system_utc_ts())

    # --- heartbeat を更新（トークン不一致等は reject） ---
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
    return schemas.ControlAgentJobMutationResponse(ok=True, job_id=str(job_id), status=str(item["status"]))


@router.post("/control/agent-jobs/{job_id}/complete", response_model=schemas.ControlAgentJobMutationResponse)
def control_agent_job_complete(
    job_id: str,
    request: schemas.ControlAgentJobCompleteRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    agent_runner の complete callback を受け付ける。
    """

    # --- アクティブ memory DB を解決 ---
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
    return schemas.ControlAgentJobMutationResponse(ok=True, job_id=str(out["job_id"]), status=str(out["status"]))


@router.post("/control/agent-jobs/{job_id}/fail", response_model=schemas.ControlAgentJobMutationResponse)
def control_agent_job_fail(
    job_id: str,
    request: schemas.ControlAgentJobFailRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    agent_runner の fail callback を受け付ける。
    """

    # --- アクティブ memory DB を解決 ---
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
    return schemas.ControlAgentJobMutationResponse(ok=True, job_id=str(out["job_id"]), status=str(out["status"]))


@router.get("/control/agent-jobs", response_model=schemas.ControlAgentJobsResponse)
def control_agent_jobs(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    backend: str | None = Query(default=None),
) -> schemas.ControlAgentJobsResponse:
    """
    agent_jobs 一覧を返す（新しい順）。
    """

    # --- repo から一覧を取得 ---
    repo = _get_autonomy_repo_from_active_config()
    items = repo.list_recent_agent_jobs(limit=int(limit), status=status, backend=backend)
    return schemas.ControlAgentJobsResponse(items=[schemas.ControlAgentJobItem(**x) for x in items])


@router.get("/control/agent-jobs/{job_id}", response_model=schemas.ControlAgentJobResponse)
def control_agent_job(
    job_id: str,
) -> schemas.ControlAgentJobResponse:
    """
    agent_job 1件を返す。
    """

    # --- repo から1件取得 ---
    repo = _get_autonomy_repo_from_active_config()
    item = repo.get_agent_job(job_id=str(job_id))
    if item is None:
        raise HTTPException(status_code=404, detail="agent job not found")
    return schemas.ControlAgentJobResponse(item=schemas.ControlAgentJobItem(**item))
