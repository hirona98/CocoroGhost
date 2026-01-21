"""
HTTP 用の認証ユーティリティ（Bearer / Cookie セッション）。

目的:
    - Web UI は Cookie セッションで認証する。
    - 既存の外部連携は Bearer トークンで認証する。

方針:
    - Bearer があれば優先して検証する。
    - Bearer が無い場合のみ Cookie を見る。
    - Cookie のセッションはメモリ保存（プロセス再起動で無効）。
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from cocoro_ghost.config import get_config_store
from cocoro_ghost.web_sessions import get_web_session_store


COOKIE_NAME = "cocoro_session"


def _verify_bearer_token_from_header(auth_header: str | None) -> bool:
    """Authorization ヘッダの Bearer を検証し、有効なら True。"""

    raw = str(auth_header or "").strip()
    if not raw:
        return False

    # --- 形式: "Bearer <TOKEN>" ---
    if not raw.lower().startswith("bearer "):
        return False
    provided = raw.split(" ", 1)[1].strip()
    expected = get_config_store().config.token
    return bool(provided and provided == expected)


def require_bearer_only(request: Request) -> None:
    """HTTP リクエストで Bearer 認証を必須にする。"""

    # --- Authorization ヘッダを検証 ---
    if _verify_bearer_token_from_header(request.headers.get("Authorization")):
        return

    # --- 失敗 ---
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")


def require_bearer_or_cookie_session(request: Request) -> None:
    """HTTP リクエストで Bearer または Cookie セッションを必須にする。"""

    # --- 1) Bearer を優先 ---
    if _verify_bearer_token_from_header(request.headers.get("Authorization")):
        return

    # --- 2) Cookie セッション ---
    sid = str(request.cookies.get(COOKIE_NAME, "")).strip()
    if sid and get_web_session_store().validate_and_touch(sid):
        return

    # --- 失敗 ---
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

