"""WebSocketによるアプリイベント（通知/メタ等）ストリーミングAPI。"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from cocoro_ghost import event_stream
from cocoro_ghost.api.ws_auth import authenticate_ws_bearer


router = APIRouter(prefix="/events", tags=["events"])


@router.websocket("/stream")
async def stream_events(websocket: WebSocket) -> None:
    """アプリイベント（通知/メタ等）をWebSocketでストリーミング配信する。"""
    if not await authenticate_ws_bearer(websocket):
        return

    await websocket.accept()
    await event_stream.add_client(websocket)
    await event_stream.send_buffer(websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await event_stream.remove_client(websocket)
