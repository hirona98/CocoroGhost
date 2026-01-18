"""
Web UI 認証 API（Cookie セッション）。

目的:
    - ブラウザは Bearer ヘッダを使えない（特に WebSocket）。
    - そのため、Web UI はログインで Cookie セッションを発行して以後の API を呼ぶ。

エンドポイント:
    - POST /api/auth/login  : token を受け取り、セッション Cookie を発行
    - POST /api/auth/logout : セッション破棄（Cookie 削除）
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from cocoro_ghost.api.http_auth import COOKIE_NAME
from cocoro_ghost.config import get_config_store
from cocoro_ghost.web_sessions import get_web_session_store


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """ログインリクエスト。"""

    token: str = Field(default="", description="Bearer と同じ共有トークン（settings.db の token）")


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
def login(request: LoginRequest) -> Response:
    """トークンが一致すれば Cookie セッションを発行する。"""

    # --- トークンを検証 ---
    provided = str(request.token or "").strip()
    expected = get_config_store().config.token
    if not provided or provided != expected:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # --- セッションを発行 ---
    session = get_web_session_store().create()

    # --- Cookie をセット ---
    # NOTE:
    # - Max-Age を短くすると「スライディング延長」が Cookie 側で表現できない。
    # - サーバ側が 24h アイドルタイムアウトで無効化するため、Cookie の寿命は長めでよい。
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=session.session_id,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60,
        path="/",
    )
    return resp


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(raw_request: Request) -> Response:
    """Cookie セッションを破棄してログアウトする。"""

    # --- Cookie から session_id を取り出す ---
    sid = str(raw_request.cookies.get(COOKIE_NAME, "")).strip()
    if sid:
        get_web_session_store().delete(sid)

    # --- Cookie を削除 ---
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(key=COOKIE_NAME, path="/")
    return resp

