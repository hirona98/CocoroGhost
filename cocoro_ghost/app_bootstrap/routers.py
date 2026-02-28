"""
HTTP ルート登録。

目的:
    - router 登録と静的ファイル配信の配線をまとめる。
    - `main.py` から HTTP 配線の詳細を外す。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cocoro_ghost.api import (
    admin,
    auth,
    chat,
    control,
    events,
    logs,
    meta_request,
    mood,
    notification,
    reminders,
    settings,
    vision,
)
from cocoro_ghost.api.http_auth import require_bearer_only, require_bearer_or_cookie_session
from cocoro_ghost.resources import get_static_dir


def register_http_routes(app: FastAPI) -> None:
    """
    API router と静的ファイルルートを登録する。
    """

    # --- 認証付き API router を登録する ---
    app.include_router(chat.router, dependencies=[Depends(require_bearer_or_cookie_session)], prefix="/api")
    app.include_router(notification.router, dependencies=[Depends(require_bearer_only)], prefix="/api")
    app.include_router(meta_request.router, dependencies=[Depends(require_bearer_only)], prefix="/api")
    app.include_router(vision.router, dependencies=[Depends(require_bearer_only)], prefix="/api")
    app.include_router(settings.router, dependencies=[Depends(require_bearer_or_cookie_session)], prefix="/api")
    app.include_router(reminders.router, dependencies=[Depends(require_bearer_only)], prefix="/api")
    app.include_router(admin.router, dependencies=[Depends(require_bearer_only)], prefix="/api")
    app.include_router(mood.router, dependencies=[Depends(require_bearer_only)], prefix="/api")
    app.include_router(control.router, dependencies=[Depends(require_bearer_only)], prefix="/api")

    # --- 認証方式ごとの例外 router を登録する ---
    app.include_router(auth.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(events.router, prefix="/api")

    # --- ヘルスチェックを登録する ---
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """稼働確認用のヘルスチェックを返す。"""

        return {"status": "healthy"}

    # --- Web UI の静的配信を登録する ---
    static_dir = get_static_dir()
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        """Web UI の index.html を返す。"""

        index_path = (static_dir / "index.html").resolve()
        return FileResponse(index_path)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        """Web UI の favicon.ico を返す。"""

        favicon_path = (static_dir / "favicon.ico").resolve()
        return FileResponse(favicon_path, media_type="image/x-icon")
