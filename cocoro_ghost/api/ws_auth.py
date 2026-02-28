"""
WebSocket用のBearer認証ユーティリティ

WebSocket接続時にAuthorizationヘッダーのBearerトークンを検証する。
このモジュールは検証だけを担当し、closeは呼び出し側が行う。
"""

from __future__ import annotations

from fastapi import WebSocket

from cocoro_ghost.config import get_config_store
from cocoro_ghost.infra.web_sessions import get_web_session_store


COOKIE_NAME = "cocoro_session"


async def authenticate_ws_bearer(websocket: WebSocket) -> bool:
    """
    WebSocket接続のBearerトークンを検証する。

    Authorizationヘッダーからトークンを取得し、設定と照合する。
    認証失敗時は False を返す（close責務は呼び出し側）。
    """
    # --- Authorization ヘッダを検証する ---
    auth_header = websocket.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return False

    provided = auth_header.split(" ", 1)[1].strip()
    expected = get_config_store().config.token
    if provided != expected:
        return False

    return True


async def authenticate_ws_bearer_or_cookie_session(websocket: WebSocket) -> bool:
    """
    WebSocket接続で Bearer または Cookie セッションを検証する。

    方針:
    - Bearer があれば優先して検証する。
    - Bearer が無い場合のみ Cookie（cocoro_session）で検証する。
    - 認証失敗時は False を返す（close責務は呼び出し側）。
    """

    # --- 1) Bearer を優先 ---
    auth_header = websocket.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return await authenticate_ws_bearer(websocket)

    # --- 2) Cookie セッション ---
    sid = str(websocket.cookies.get(COOKIE_NAME, "")).strip()
    if sid and get_web_session_store().validate_and_touch(sid):
        return True

    # --- 失敗 ---
    return False
