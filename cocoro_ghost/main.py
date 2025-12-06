"""FastAPI エントリポイント。"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_utils.tasks import repeat_every

from cocoro_ghost.api import capture, chat, meta_request, notification, settings
from cocoro_ghost.cleanup import cleanup_old_images
from cocoro_ghost.config import get_config_store
from cocoro_ghost.logging_config import setup_logging


security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = get_config_store().config.token
    if credentials.credentials != token:
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
        load_active_character_preset,
        load_active_llm_preset,
        load_global_settings,
        migrate_toml_to_v2_if_needed,
        settings_session_scope,
    )

    # 1. TOML設定読み込み
    toml_config = load_config()
    setup_logging(toml_config.log_level)

    # 2. 設定DB初期化
    init_settings_db()

    # 3. マイグレーション（旧SettingPreset -> 新テーブル or TOML -> 新テーブル）
    with settings_session_scope() as session:
        migrate_toml_to_v2_if_needed(session, toml_config)

    # 4. アクティブなプリセットを読み込み
    with settings_session_scope() as session:
        global_settings = load_global_settings(session)
        llm_preset = load_active_llm_preset(session)
        character_preset = load_active_character_preset(session)

        # RuntimeConfig構築
        runtime_config = build_runtime_config(
            toml_config, global_settings, llm_preset, character_preset
        )

        # ConfigStore作成（プリセットオブジェクトをデタッチ状態で保持するためコピー）
        # SQLAlchemyセッション終了後も使えるようにexpungeする
        session.expunge(global_settings)
        session.expunge(llm_preset)
        session.expunge(character_preset)

        config_store = ConfigStore(
            toml_config, runtime_config, global_settings, llm_preset, character_preset
        )

    set_global_config_store(config_store)

    # 5. 記憶DB初期化（memory_idに対応）
    init_memory_db(runtime_config.memory_id, runtime_config.embedding_dimension)

    # 6. FastAPIアプリ作成
    app = FastAPI(title="CocoroGhost API")

    app.include_router(chat.router, dependencies=[Depends(verify_token)])
    app.include_router(notification.router, dependencies=[Depends(verify_token)])
    app.include_router(meta_request.router, dependencies=[Depends(verify_token)])
    app.include_router(capture.router, dependencies=[Depends(verify_token)])
    app.include_router(settings.router, dependencies=[Depends(verify_token)])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def root():
        return {"message": "CocoroGhost API is running"}

    @app.on_event("startup")
    @repeat_every(seconds=600, wait_first=True)
    async def periodic_cleanup() -> None:
        cleanup_old_images()

    return app


app = create_app()
