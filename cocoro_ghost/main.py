"""FastAPI エントリポイント。"""

from __future__ import annotations

import asyncio
import os
import threading

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_utils.tasks import repeat_every

from cocoro_ghost import event_stream, log_stream
from cocoro_ghost.api import admin, capture, chat, events, logs, meta_request, notification, settings
from cocoro_ghost.cleanup import cleanup_old_images
from cocoro_ghost.config import get_config_store
from cocoro_ghost.logging_config import setup_logging, suppress_uvicorn_access_log_paths


security = HTTPBearer()
logger = __import__("logging").getLogger(__name__)

_internal_worker_thread: threading.Thread | None = None
_internal_worker_stop_event: threading.Event | None = None


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = get_config_store().config.token
    if credentials.credentials != token:
        logger.warning("Authentication failed: invalid token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")
    return credentials.credentials


def create_app() -> FastAPI:
    from cocoro_ghost.config import (
        ConfigStore,
        build_runtime_config,
        load_config,
        set_global_config_store,
    )
    from cocoro_ghost.db import (
        init_memory_db,
        init_settings_db,
        load_active_embedding_preset,
        load_active_contract_preset,
        load_active_llm_preset,
        load_active_persona_preset,
        load_global_settings,
        ensure_initial_settings,
        settings_session_scope,
    )

    # 1. TOML設定読み込み
    toml_config = load_config()
    setup_logging(toml_config.log_level)
    suppress_uvicorn_access_log_paths("/api/health")

    # 2. 設定DB初期化
    init_settings_db()

    # 3. 初期設定レコードの作成
    with settings_session_scope() as session:
        ensure_initial_settings(session, toml_config)

    # 4. アクティブなプリセットを読み込み
    with settings_session_scope() as session:
        global_settings = load_global_settings(session)
        llm_preset = load_active_llm_preset(session)
        embedding_preset = load_active_embedding_preset(session)
        persona_preset = load_active_persona_preset(session)
        contract_preset = load_active_contract_preset(session)

        # RuntimeConfig構築
        runtime_config = build_runtime_config(
            toml_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            contract_preset,
        )

        # ConfigStore作成（プリセットオブジェクトをデタッチ状態で保持するためコピー）
        # SQLAlchemyセッション終了後も使えるようにexpungeする
        session.expunge(global_settings)
        session.expunge(llm_preset)
        session.expunge(embedding_preset)
        session.expunge(persona_preset)
        session.expunge(contract_preset)

        config_store = ConfigStore(
            toml_config,
            runtime_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            contract_preset,
        )

    set_global_config_store(config_store)

    # 5. 記憶DB初期化（memory_idに対応）
    init_memory_db(runtime_config.memory_id, runtime_config.embedding_dimension)

    # 6. FastAPIアプリ作成
    app = FastAPI(title="CocoroGhost API")

    app.include_router(chat.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(notification.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(meta_request.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(capture.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(settings.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(admin.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(events.router, prefix="/api")

    @app.get("/api/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/")
    async def root():
        return {"message": "CocoroGhost API is running"}

    @app.on_event("startup")
    @repeat_every(seconds=600, wait_first=True)
    async def periodic_cleanup() -> None:
        cleanup_old_images()

    @app.on_event("startup")
    async def start_log_stream_dispatcher() -> None:
        loop = asyncio.get_running_loop()
        log_stream.install_log_handler(loop)
        await log_stream.start_dispatcher()

    @app.on_event("startup")
    async def start_event_stream_dispatcher() -> None:
        loop = asyncio.get_running_loop()
        event_stream.install(loop)
        await event_stream.start_dispatcher()

    @app.on_event("startup")
    async def start_internal_worker() -> None:
        # 既定は「同一プロセス内でWorkerも動かす」。別プロセス運用したい場合は無効化する。
        enabled = str(os.getenv("COCORO_GHOST_INTERNAL_WORKER", "1")).strip().lower()
        if enabled in {"0", "false", "off", "no"}:
            logger.info("internal worker disabled by env", extra={"env": "COCORO_GHOST_INTERNAL_WORKER"})
            return

        global _internal_worker_thread, _internal_worker_stop_event
        if _internal_worker_thread is not None and _internal_worker_thread.is_alive():
            return

        from cocoro_ghost.deps import get_llm_client
        from cocoro_ghost.worker import run_forever

        stop_event = threading.Event()
        llm_client = get_llm_client()

        t = threading.Thread(
            target=run_forever,
            kwargs={
                "memory_id": runtime_config.memory_id,
                "embedding_dimension": runtime_config.embedding_dimension,
                "llm_client": llm_client,
                # 固定値（cron無し定期enqueue込み）
                "poll_interval_seconds": 1.0,
                "max_jobs_per_tick": 10,
                "periodic_interval_seconds": 30.0,
                "stop_event": stop_event,
            },
            name="cocoro_ghost_internal_worker",
            daemon=True,
        )
        _internal_worker_stop_event = stop_event
        _internal_worker_thread = t
        t.start()
        logger.info("internal worker started", extra={"memory_id": runtime_config.memory_id})

    @app.on_event("shutdown")
    async def stop_log_stream_dispatcher() -> None:
        await log_stream.stop_dispatcher()

    @app.on_event("shutdown")
    async def stop_event_stream_dispatcher() -> None:
        await event_stream.stop_dispatcher()

    @app.on_event("shutdown")
    async def stop_internal_worker() -> None:
        global _internal_worker_thread, _internal_worker_stop_event
        if _internal_worker_stop_event is not None:
            _internal_worker_stop_event.set()
        if _internal_worker_thread is not None:
            await asyncio.to_thread(_internal_worker_thread.join, 5.0)
        _internal_worker_thread = None
        _internal_worker_stop_event = None

    return app


app = create_app()
