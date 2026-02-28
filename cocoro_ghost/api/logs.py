"""
WebSocketによるログストリーミングAPI

アプリケーションログをリアルタイムでクライアントに配信する。
デバッグやモニタリング目的でCocoroConsole等から購読される。
ログはバッファリングされ、新規接続時に直近のログも送信される。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from cocoro_ghost.runtime import log_stream
from cocoro_ghost.api.ws_auth import authenticate_ws_bearer


router = APIRouter(prefix="/logs", tags=["logs"])
logger = logging.getLogger(__name__)


async def _close_policy_violation(websocket: WebSocket) -> None:
    """
    認証失敗などのポリシー違反でWebSocketを閉じる。

    close時例外は制御系のため、debugで記録して握りつぶす。
    """

    # --- policy violation で close する ---
    try:
        await websocket.close(code=1008)
    except Exception as exc:  # noqa: BLE001
        logger.debug("logs websocket close failed: %s", str(exc))


@router.websocket("/stream")
async def stream_logs(websocket: WebSocket) -> None:
    """
    アプリログをWebSocketでストリーミング配信する。

    Bearer認証後に接続を受け入れ、バッファ済みログを送信してから
    新規ログをリアルタイムで配信する。切断時は自動でクライアント登録解除。
    """
    await websocket.accept()
    if not await authenticate_ws_bearer(websocket):
        await _close_policy_violation(websocket)
        logger.info("logs websocket rejected (auth failed)")
        return

    client_added = False
    try:
        # --- 購読クライアントとして登録し、リングバッファを送る ---
        await log_stream.add_client(websocket)
        client_added = True
        await log_stream.send_buffer(websocket)
        logger.info("logs websocket connected")

        # --- keepalive受信ループ ---
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("logs websocket disconnected by client")
    except Exception as exc:  # noqa: BLE001
        logger.warning("logs websocket terminated by error: %s", str(exc))
    finally:
        if client_added:
            await log_stream.remove_client(websocket)
        logger.info("logs websocket disconnected")
