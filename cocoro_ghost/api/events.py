"""
WebSocketによるアプリイベントストリーミングAPI

アプリケーションイベント（通知完了、メタ要求完了等）をリアルタイムで配信する。
クライアント（CocoroConsole等）はこのストリームを購読して、
非同期処理の完了を受け取ることができる。
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from cocoro_ghost import event_stream
from cocoro_ghost.api.ws_auth import authenticate_ws_bearer


router = APIRouter(prefix="/events", tags=["events"])


@router.websocket("/stream")
async def stream_events(websocket: WebSocket) -> None:
    """
    アプリイベントをWebSocketでストリーミング配信する。

    通知完了、メタ要求完了などのイベントをリアルタイムで配信する。
    Bearer認証後に接続を受け入れ、切断時は自動でクライアント登録解除。
    """
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
