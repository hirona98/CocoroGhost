"""
/control ルーター。

目的:
    - ルーティングと入出力定義だけを保持する。
    - 業務ロジックは services.control_service へ委譲する。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from cocoro_ghost import schemas
from cocoro_ghost.api.services import control_service
from cocoro_ghost.app_bootstrap.dependencies import get_clock_service_dep
from cocoro_ghost.core.clock import ClockService

router = APIRouter()


@router.post("/control", status_code=status.HTTP_204_NO_CONTENT)
def control(
    request: schemas.ControlRequest,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    プロセス制御コマンドを受け付ける。

    現状は shutdown のみをサポートし、受理後にプロセス終了を要求する。
    """
    return control_service.schedule_process_control(
        request=request,
        background_tasks=background_tasks,
    )


@router.get("/control/stream-stats", response_model=schemas.StreamRuntimeStatsResponse)
def stream_stats() -> schemas.StreamRuntimeStatsResponse:
    """
    ストリームのランタイム統計を返す。

    運用時に queue逼迫/ドロップ/送信失敗を確認するために使う。
    """
    return control_service.build_stream_stats_response()


@router.get("/control/worker-stats", response_model=schemas.WorkerRuntimeStatsResponse)
def worker_stats() -> schemas.WorkerRuntimeStatsResponse:
    """
    Workerのジョブキュー統計を返す。

    pending/running/stale の詰まり具合を運用時に確認するために使う。
    """
    return control_service.build_worker_stats_response()


@router.get("/control/time", response_model=schemas.ControlTimeSnapshotResponse)
def control_time_snapshot(
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlTimeSnapshotResponse:
    """
    時刻スナップショットを返す。

    - system: OS実時間
    - domain: 会話/記憶/感情の評価に使う論理時刻
    """
    return control_service.get_time_snapshot(clock_service=clock_service)


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
    return control_service.advance_time(
        request=request,
        clock_service=clock_service,
    )


@router.post("/control/time/reset", response_model=schemas.ControlTimeSnapshotResponse)
def control_time_reset(
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlTimeSnapshotResponse:
    """
    domain時刻オフセットをリセットする。

    system時刻には影響しない。
    """
    return control_service.reset_time(clock_service=clock_service)


@router.get("/control/autonomy/status", response_model=schemas.ControlAutonomyStatusResponse)
def control_autonomy_status() -> schemas.ControlAutonomyStatusResponse:
    """
    自発行動の稼働状態と滞留数を返す。
    """
    return control_service.get_autonomy_status()


@router.post("/control/autonomy/start", response_model=schemas.ControlAutonomyToggleResponse)
def control_autonomy_start() -> schemas.ControlAutonomyToggleResponse:
    """
    自発行動を有効化する。
    """
    return control_service.set_autonomy_enabled(enabled=True)


@router.post("/control/autonomy/stop", response_model=schemas.ControlAutonomyToggleResponse)
def control_autonomy_stop() -> schemas.ControlAutonomyToggleResponse:
    """
    自発行動を無効化する。
    """
    return control_service.set_autonomy_enabled(enabled=False)


@router.post("/control/autonomy/trigger", response_model=schemas.ControlAutonomyToggleResponse)
def control_autonomy_trigger(request: schemas.ControlAutonomyTriggerRequest) -> schemas.ControlAutonomyToggleResponse:
    """
    手動トリガを投入する（デバッグ用）。
    """
    return control_service.trigger_autonomy(request=request)


@router.get("/control/autonomy/intents", response_model=schemas.ControlAutonomyIntentsResponse)
def control_autonomy_intents(
    limit: int = Query(default=50, ge=1, le=500),
) -> schemas.ControlAutonomyIntentsResponse:
    """
    intent 一覧を返す。
    """
    return control_service.list_autonomy_intents(limit=int(limit))


@router.post("/control/agent-jobs/claim", response_model=schemas.ControlAgentJobClaimResponse)
def control_agent_jobs_claim(
    request: schemas.ControlAgentJobClaimRequest,
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlAgentJobClaimResponse:
    """
    agent_runner 向けに queued agent_job を claim して返す。
    """
    return control_service.claim_agent_jobs(
        request=request,
        clock_service=clock_service,
    )


@router.post("/control/agent-jobs/{job_id}/heartbeat", response_model=schemas.ControlAgentJobMutationResponse)
def control_agent_job_heartbeat(
    job_id: str,
    request: schemas.ControlAgentJobHeartbeatRequest,
    clock_service: ClockService = Depends(get_clock_service_dep),
) -> schemas.ControlAgentJobMutationResponse:
    """
    実行中 agent_job の heartbeat を更新する。
    """
    return control_service.heartbeat_agent_job(
        job_id=str(job_id),
        request=request,
        clock_service=clock_service,
    )


@router.post("/control/agent-jobs/{job_id}/complete", response_model=schemas.ControlAgentJobMutationResponse)
def control_agent_job_complete(
    job_id: str,
    request: schemas.ControlAgentJobCompleteRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    agent_runner の complete callback を受け付ける。
    """
    return control_service.complete_agent_job(
        job_id=str(job_id),
        request=request,
    )


@router.post("/control/agent-jobs/{job_id}/fail", response_model=schemas.ControlAgentJobMutationResponse)
def control_agent_job_fail(
    job_id: str,
    request: schemas.ControlAgentJobFailRequest,
) -> schemas.ControlAgentJobMutationResponse:
    """
    agent_runner の fail callback を受け付ける。
    """
    return control_service.fail_agent_job(
        job_id=str(job_id),
        request=request,
    )


@router.get("/control/agent-jobs", response_model=schemas.ControlAgentJobsResponse)
def control_agent_jobs(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    backend: str | None = Query(default=None),
) -> schemas.ControlAgentJobsResponse:
    """
    agent_jobs 一覧を返す（新しい順）。
    """
    return control_service.list_agent_jobs(
        limit=int(limit),
        job_status=status,
        backend=backend,
    )


@router.get("/control/agent-jobs/{job_id}", response_model=schemas.ControlAgentJobResponse)
def control_agent_job(
    job_id: str,
) -> schemas.ControlAgentJobResponse:
    """
    agent_job 1件を返す。
    """
    return control_service.get_agent_job(job_id=str(job_id))
