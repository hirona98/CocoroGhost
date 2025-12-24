"""FastAPI エントリポイント。"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_utils.tasks import repeat_every

from cocoro_ghost import event_stream, log_stream
from cocoro_ghost.api import admin, capture, chat, events, logs, meta_request, mood, notification, settings
from cocoro_ghost.cleanup import cleanup_old_images
from cocoro_ghost.config import get_config_store
from cocoro_ghost.logging_config import setup_logging, suppress_uvicorn_access_log_paths


security = HTTPBearer()
logger = __import__("logging").getLogger(__name__)


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Bearerトークンを検証し、OKならトークン文字列を返す。"""
    token = get_config_store().config.token
    if credentials.credentials != token:
        logger.warning("Authentication failed: invalid token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")
    return credentials.credentials


def create_app() -> FastAPI:
    """アプリ生成と初期化（設定DB→プリセット→記憶DB→ルータ登録）をまとめて行う。"""
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
        load_active_addon_preset,
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
        addon_preset = load_active_addon_preset(session)

        # RuntimeConfig構築
        runtime_config = build_runtime_config(
            toml_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            addon_preset,
        )

        # ConfigStore作成（プリセットオブジェクトをデタッチ状態で保持するためコピー）
        # SQLAlchemyセッション終了後も使えるようにexpungeする
        session.expunge(global_settings)
        session.expunge(llm_preset)
        session.expunge(embedding_preset)
        session.expunge(persona_preset)
        session.expunge(addon_preset)

        config_store = ConfigStore(
            toml_config,
            runtime_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            addon_preset,
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
    app.include_router(mood.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(settings.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(admin.router, dependencies=[Depends(verify_token)], prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(events.router, prefix="/api")

    @app.get("/api/health")
    async def health():
        """稼働確認用のヘルスチェック。"""
        return {"status": "healthy"}

    @app.get("/")
    async def root():
        """ルートの簡易応答（動作確認用）。"""
        return {"message": "CocoroGhost API is running"}

    @app.on_event("startup")
    @repeat_every(seconds=600, wait_first=True)
    async def periodic_cleanup() -> None:
        """定期的な不要ファイル掃除（画像など）。"""
        cleanup_old_images()

    @app.on_event("startup")
    async def start_log_stream_dispatcher() -> None:
        """ログSSE配信のdispatcherを起動。"""
        loop = asyncio.get_running_loop()
        log_stream.install_log_handler(loop)
        await log_stream.start_dispatcher()

    @app.on_event("startup")
    async def start_event_stream_dispatcher() -> None:
        """イベントSSE配信のdispatcherを起動。"""
        loop = asyncio.get_running_loop()
        event_stream.install(loop)
        await event_stream.start_dispatcher()

    @app.on_event("startup")
    async def start_internal_worker() -> None:
        """同一プロセス内のWorkerスレッドを起動（jobsテーブルを処理）。"""
        from cocoro_ghost import internal_worker

        internal_worker.start(memory_id=runtime_config.memory_id, embedding_dimension=runtime_config.embedding_dimension)
        logger.info("internal worker started", extra={"memory_id": runtime_config.memory_id})

    @app.on_event("shutdown")
    async def stop_log_stream_dispatcher() -> None:
        """ログSSE配信のdispatcherを停止。"""
        await log_stream.stop_dispatcher()

    @app.on_event("shutdown")
    async def stop_event_stream_dispatcher() -> None:
        """イベントSSE配信のdispatcherを停止。"""
        await event_stream.stop_dispatcher()

    @app.on_event("shutdown")
    async def stop_internal_worker() -> None:
        """同一プロセス内Workerスレッドを停止。"""
        from cocoro_ghost import internal_worker

        # internal_worker.stop は keyword-only の timeout_seconds を受け取る
        await asyncio.to_thread(internal_worker.stop, timeout_seconds=5.0)

    return app


app = create_app()
