"""
WebSocket用のBearer認証ユーティリティ

WebSocket接続時にAuthorizationヘッダーのBearerトークンを検証する。
認証失敗時はWS_1008_POLICY_VIOLATIONでコネクションをクローズする。
"""

from __future__ import annotations

from fastapi import WebSocket, status

from cocoro_ghost.config import get_config_store


async def authenticate_ws_bearer(websocket: WebSocket) -> bool:
    """
    WebSocket接続のBearerトークンを検証する。

    Authorizationヘッダーからトークンを取得し、設定と照合する。
    認証失敗時はWS_1008_POLICY_VIOLATIONでクローズしてFalseを返す。
    """
    # WebSocketはHTTPステータスを返せないため、認証失敗は規約違反としてcloseする
    auth_header = websocket.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    provided = auth_header.split(" ", 1)[1].strip()
    expected = get_config_store().config.token
    if provided != expected:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    return True
