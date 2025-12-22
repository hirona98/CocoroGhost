"""WebSocket用のBearer認証ユーティリティ。

"""

from __future__ import annotations

from fastapi import WebSocket, status

from cocoro_ghost.config import get_config_store


async def authenticate_ws_bearer(websocket: WebSocket) -> bool:
    """Authorization: Bearer <TOKEN> を検証し、NGならcloseしてFalseを返す。"""

    # WebSocketはHTTPステータスを返せないため、認証失敗は規約違反としてcloseする。
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
