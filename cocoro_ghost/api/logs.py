"""WebSocketによるログストリーミングAPI。"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from cocoro_ghost import log_stream
from cocoro_ghost.api.ws_auth import authenticate_ws_bearer


router = APIRouter(prefix="/logs", tags=["logs"])


@router.websocket("/stream")
async def stream_logs(websocket: WebSocket) -> None:
    """アプリログをWebSocketでストリーミング配信する。"""
    if not await authenticate_ws_bearer(websocket):
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
