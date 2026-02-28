"""
WebSocketによるアプリイベントストリーミングAPI

アプリケーションイベント（通知完了、メタ要求完了等）をリアルタイムで配信する。
クライアント（CocoroConsole等）はこのストリームを購読して、
非同期処理の完了を受け取ることができる。

Planned:
- 視覚（Vision）のための命令（capture_request）も同じストリームで配信する。
- クライアントは接続直後に hello を送って client_id を登録する。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from cocoro_ghost.runtime import event_stream
from cocoro_ghost.api.ws_auth import authenticate_ws_bearer_or_cookie_session


router = APIRouter(prefix="/events", tags=["events"])
logger = logging.getLogger(__name__)


async def _close_policy_violation(websocket: WebSocket) -> None:
    """
    認証失敗などのポリシー違反でWebSocketを閉じる。

    close時例外は通知経路ではなく制御経路のため、debugで記録して握りつぶす。
    """

    # --- policy violation で close する ---
    try:
        await websocket.close(code=1008)
    except Exception as exc:  # noqa: BLE001
        logger.debug("events websocket close failed: %s", str(exc))


@router.websocket("/stream")
async def stream_events(websocket: WebSocket) -> None:
    """
    アプリイベントをWebSocketでストリーミング配信する。

    通知完了、メタ要求完了などのイベントをリアルタイムで配信する。
    Bearer認証後に接続を受け入れ、切断時は自動でクライアント登録解除。
    """
    # --- 接続を受け入れる ---
    # NOTE:
    # - 認証失敗時に accept せず return すると、サーバ側ログが 403 で埋まりやすい（再接続ループ時）。
    # - 先に accept し、認証NGなら policy violation(1008) で close することで、
    #   クライアント側が「auth failed」として扱いやすくする。
    await websocket.accept()

    # --- 認証を検証 ---
    if not await authenticate_ws_bearer_or_cookie_session(websocket):
        await _close_policy_violation(websocket)
        logger.info("events websocket rejected (auth failed)")
        return

    client_added = False
    try:
        # --- 購読クライアントとして登録する ---
        await event_stream.add_client(websocket)
        client_added = True
        logger.info("events websocket connected")

        # --- クライアントメッセージ受信ループ ---
        while True:
            text = await websocket.receive_text()
            # --- Client -> Ghost メッセージ（任意） ---
            # 現状は hello のみを受け付ける（将来拡張）。
            try:
                payload = json.loads(text or "")
            except json.JSONDecodeError:
                logger.debug("events websocket ignored invalid json payload")
                continue
            if not isinstance(payload, dict):
                continue

            msg_type = str(payload.get("type") or "").strip()
            if msg_type != "hello":
                continue

            client_id = str(payload.get("client_id") or "").strip()
            caps = payload.get("caps") or []
            caps_list = [str(x) for x in caps] if isinstance(caps, list) else []
            if client_id:
                event_stream.register_client_identity(websocket, client_id=client_id, caps=caps_list)
                logger.info("events websocket hello received client_id=%s caps=%s", client_id, caps_list)
    except WebSocketDisconnect:
        logger.info("events websocket disconnected by client")
    except Exception as exc:  # noqa: BLE001
        logger.warning("events websocket terminated by error: %s", str(exc))
    finally:
        if client_added:
            await event_stream.remove_client(websocket)
        logger.info("events websocket disconnected")
