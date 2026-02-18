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

from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.clock import ClockService
from cocoro_ghost import event_stream, log_stream, schemas, worker
from cocoro_ghost.db import load_global_settings, settings_session_scope
from cocoro_ghost.deps import get_clock_service_dep
from cocoro_ghost.time_utils import format_iso8601_local_with_tz

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
