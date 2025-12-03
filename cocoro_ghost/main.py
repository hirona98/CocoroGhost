"""FastAPI エントリポイント。"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi_utils.tasks import repeat_every
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cocoro_ghost.api import capture, chat, episodes, meta_request, notification, settings
from cocoro_ghost.cleanup import cleanup_old_images
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import init_db
from cocoro_ghost.logging_config import setup_logging


security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = get_config_store().config.token
    if credentials.credentials != token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")
    return credentials.credentials


def create_app() -> FastAPI:
    cfg = get_config_store()
    setup_logging(cfg.config.log_level)
    init_db(cfg.config.db_url)

    app = FastAPI(title="cocoro_ghost API")

    app.include_router(chat.router, dependencies=[Depends(verify_token)])
    app.include_router(notification.router, dependencies=[Depends(verify_token)])
    app.include_router(meta_request.router, dependencies=[Depends(verify_token)])
    app.include_router(capture.router, dependencies=[Depends(verify_token)])
    app.include_router(episodes.router, dependencies=[Depends(verify_token)])
    app.include_router(settings.router, dependencies=[Depends(verify_token)])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def root():
        return {"message": "cocoro_ghost API is running"}

    @app.on_event("startup")
    @repeat_every(seconds=600, wait_first=True)
    async def periodic_cleanup() -> None:
        cleanup_old_images()

    return app


app = create_app()
