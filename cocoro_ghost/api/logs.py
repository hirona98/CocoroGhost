"""WebSocketによるログストリーミングAPI。"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from cocoro_ghost import log_stream
from cocoro_ghost.config import get_config_store


router = APIRouter(prefix="/logs", tags=["logs"])


async def _authenticate(websocket: WebSocket) -> bool:
    """Authorizationヘッダを用いた簡易認証。"""
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


@router.websocket("/stream")
async def stream_logs(websocket: WebSocket) -> None:
    if not await _authenticate(websocket):
        return

    await websocket.accept()
    await log_stream.add_client(websocket)
    await log_stream.send_buffer(websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await log_stream.remove_client(websocket)
