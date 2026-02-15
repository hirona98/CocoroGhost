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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy import func

from cocoro_ghost.autonomy import runtime_control
from cocoro_ghost.clock import ClockService
from cocoro_ghost import event_stream, log_stream, schemas, worker
from cocoro_ghost.deps import get_clock_service_dep
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import Job
from cocoro_ghost.time_utils import format_iso8601_local_with_tz
from cocoro_ghost.worker_constants import JOB_PENDING, JOB_RUNNING

logger = __import__("logging").getLogger(__name__)

router = APIRouter()


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


def _build_autonomy_runtime_response() -> schemas.ControlAutonomyResponse:
    """自律ループの設定/稼働状態をレスポンス化する。"""

    # --- runtime 設定を取得 ---
    state = runtime_control.get_runtime_state()

    # --- アクティブ embedding 設定で run_autonomy_cycle の件数を集計 ---
    from cocoro_ghost.config import get_config_store

    cfg = get_config_store().config
    with memory_session_scope(str(cfg.embedding_preset_id), int(cfg.embedding_dimension)) as db:
        pending_jobs = (
            db.query(func.count(Job.id))
            .filter(Job.kind == "run_autonomy_cycle")
            .filter(Job.status == int(JOB_PENDING))
            .scalar()
        )
        running_jobs = (
            db.query(func.count(Job.id))
            .filter(Job.kind == "run_autonomy_cycle")
            .filter(Job.status == int(JOB_RUNNING))
            .scalar()
        )

    # --- 時刻をISOへ整形 ---
    last_started_iso = (
        format_iso8601_local_with_tz(int(state.last_cycle_started_at))
        if state.last_cycle_started_at is not None
        else None
    )
    last_finished_iso = (
        format_iso8601_local_with_tz(int(state.last_cycle_finished_at))
        if state.last_cycle_finished_at is not None
        else None
    )

    # --- レスポンスへ詰め替える ---
    return schemas.ControlAutonomyResponse(
        enabled=bool(state.enabled),
        periodic_interval_seconds=int(state.periodic_interval_seconds),
        pending_jobs=int(pending_jobs or 0),
        running_jobs=int(running_jobs or 0),
        last_cycle_started_at=last_started_iso,
        last_cycle_finished_at=last_finished_iso,
        last_cycle_status=(str(state.last_cycle_status) if state.last_cycle_status else None),
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


@router.get("/control/autonomy", response_model=schemas.ControlAutonomyResponse)
def control_autonomy_get() -> schemas.ControlAutonomyResponse:
    """
    自律ループの設定と稼働状態を返す。
    """

    # --- 現在状態を返す ---
    return _build_autonomy_runtime_response()


@router.put("/control/autonomy", response_model=schemas.ControlAutonomyResponse)
def control_autonomy_put(request: schemas.ControlAutonomyUpdateRequest) -> schemas.ControlAutonomyResponse:
    """
    自律ループ設定（enabled/periodic_interval_seconds）を更新する。
    """

    # --- 入力を反映 ---
    try:
        runtime_control.update_runtime_control(
            enabled=bool(request.enabled),
            periodic_interval_seconds=int(request.periodic_interval_seconds),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- 変更後状態を返す ---
    return _build_autonomy_runtime_response()
