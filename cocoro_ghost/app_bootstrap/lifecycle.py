"""
アプリライフサイクル登録。

目的:
    - startup / shutdown の副作用を 1 箇所へ集約する。
    - `main.py` は登録呼び出しだけにする。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from cocoro_ghost.runtime import event_stream, log_stream
from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.vision.camera_watch import get_camera_watch_service
from cocoro_ghost.config import RuntimeConfig
from cocoro_ghost.vision.desktop_watch import get_desktop_watch_service
from cocoro_ghost.runtime.logging import suppress_uvicorn_access_log_paths
from cocoro_ghost.runtime.periodic import start_periodic_task, stop_periodic_tasks
from cocoro_ghost.reminders.service import get_reminder_service


logger = logging.getLogger(__name__)


def register_lifecycle_hooks(app: FastAPI, *, runtime_config: RuntimeConfig) -> None:
    """
    FastAPI の startup / shutdown フックを登録する。
    """

    # --- access log のノイズ抑制は startup 時に確実に付与する ---
    @app.on_event("startup")
    async def suppress_noisy_uvicorn_access_logs() -> None:
        """頻繁なアクセスログを uvicorn.access から除外する。"""

        suppress_uvicorn_access_log_paths(
            "/api/health",
            "/api/mood/debug",
            "/favicon.ico",
        )

    # --- ログ SSE 配信を起動する ---
    @app.on_event("startup")
    async def start_log_stream_dispatcher() -> None:
        """ログ SSE 配信を起動する。"""

        loop = asyncio.get_running_loop()
        log_stream.install_log_handler(loop)
        await log_stream.start_dispatcher()

    # --- イベント SSE 配信を起動する ---
    @app.on_event("startup")
    async def start_event_stream_dispatcher() -> None:
        """イベント SSE 配信を起動する。"""

        loop = asyncio.get_running_loop()
        event_stream.install(loop)
        await event_stream.start_dispatcher()

    # --- 内蔵 Worker を起動する ---
    @app.on_event("startup")
    async def start_internal_worker() -> None:
        """同一プロセス内 Worker を起動する。"""

        from cocoro_ghost.jobs import internal_worker

        internal_worker.start(
            embedding_preset_id=runtime_config.embedding_preset_id,
            embedding_dimension=runtime_config.embedding_dimension,
        )
        logger.info(
            "internal worker start requested",
            extra={
                "embedding_preset_id": runtime_config.embedding_preset_id,
                "memory_enabled": runtime_config.memory_enabled,
            },
        )

    # --- 定期サービスを起動する ---
    @app.on_event("startup")
    async def start_periodic_services() -> None:
        """デスクトップウォッチ、カメラ、リマインダー、自発行動を起動する。"""

        # --- event_stream 起動後に periodic を登録し、 publish レースを避ける ---
        async def _desktop_watch_tick() -> None:
            service = get_desktop_watch_service()
            await asyncio.to_thread(service.tick)

        start_periodic_task(
            app,
            name="periodic_desktop_watch",
            interval_seconds=1.0,
            wait_first=True,
            func=_desktop_watch_tick,
            logger=logger,
        )

        async def _camera_watch_tick() -> None:
            service = get_camera_watch_service()
            await asyncio.to_thread(service.tick)

        start_periodic_task(
            app,
            name="periodic_camera_watch",
            interval_seconds=1.0,
            wait_first=True,
            func=_camera_watch_tick,
            logger=logger,
        )

        async def _reminders_tick() -> None:
            service = get_reminder_service()
            await asyncio.to_thread(service.tick)

        start_periodic_task(
            app,
            name="periodic_reminders",
            interval_seconds=1.0,
            wait_first=True,
            func=_reminders_tick,
            logger=logger,
        )

        async def _autonomy_tick() -> None:
            orchestrator = get_autonomy_orchestrator()
            await asyncio.to_thread(orchestrator.tick)

        start_periodic_task(
            app,
            name="periodic_autonomy",
            interval_seconds=1.0,
            wait_first=True,
            func=_autonomy_tick,
            logger=logger,
        )
        logger.info("periodic services started")

    # --- ログ SSE を停止する ---
    @app.on_event("shutdown")
    async def stop_log_stream_dispatcher() -> None:
        """ログ SSE 配信を停止する。"""

        await log_stream.stop_dispatcher()

    # --- periodic を先に止めて publish レースを避ける ---
    @app.on_event("shutdown")
    async def stop_periodic_services() -> None:
        """定期実行タスクを停止する。"""

        await stop_periodic_tasks(app, logger=logger)

    # --- イベント SSE を停止する ---
    @app.on_event("shutdown")
    async def stop_event_stream_dispatcher() -> None:
        """イベント SSE 配信を停止する。"""

        await event_stream.stop_dispatcher()

    # --- 内蔵 Worker を最後に停止する ---
    @app.on_event("shutdown")
    async def stop_internal_worker() -> None:
        """同一プロセス内 Worker を停止する。"""

        from cocoro_ghost.jobs import internal_worker

        await asyncio.to_thread(internal_worker.stop, timeout_seconds=5.0)
