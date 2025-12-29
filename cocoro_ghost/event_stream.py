"""
WebSocket向けアプリイベント配信

通知完了、メタ要求完了などのアプリケーションイベントを
WebSocketクライアントにリアルタイム配信する。
イベントはリングバッファに保持され、新規接続時にキャッチアップ可能。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket


MAX_BUFFER = 200


@dataclass
class AppEvent:
    """
    WebSocket配信用のイベント。

    メモリID・UnitIDに紐づく通知データを保持する。
    """

    event_id: str  # イベント固有ID（UUID）
    ts: str  # ISO形式タイムスタンプ
    type: str  # イベント種別（notification_done, meta_done等）
    embedding_preset_id: str  # 対象EmbeddingPreset ID（= 記憶DB識別子）
    unit_id: int  # 関連UnitID
    data: Dict[str, Any]  # 追加データ


_event_queue: Optional[asyncio.Queue[AppEvent]] = None
_buffer: Deque[AppEvent] = deque(maxlen=MAX_BUFFER)
_clients: Set["WebSocket"] = set()
_dispatch_task: Optional[asyncio.Task[None]] = None
_handler_installed = False
_loop: Optional[asyncio.AbstractEventLoop] = None
logger = logging.getLogger(__name__)


def _serialize_event(event: AppEvent) -> str:
    """
    イベントをJSON文字列にシリアライズする。

    WebSocket送信用の最小ペイロードに整形する。
    """
    return json.dumps(
        {
            "unit_id": event.unit_id,
            "type": event.type,
            "data": event.data,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def install(loop: asyncio.AbstractEventLoop) -> None:
    """
    イベントストリームを初期化する。

    publish()で使用するイベントループとキューをセットアップする。
    多重呼び出しは無視される。
    """
    global _event_queue, _handler_installed, _loop
    if _handler_installed:
        return
    _loop = loop
    _event_queue = asyncio.Queue()
    _handler_installed = True
    logger.info("event stream installed")


async def start_dispatcher() -> None:
    """
    イベント配信タスクを起動する。

    キューからイベントを取り出し、接続中の全クライアントへ配信する。
    """
    global _dispatch_task
    if _dispatch_task is not None:
        return
    if _event_queue is None:
        raise RuntimeError("event queue is not initialized. call install() first.")
    loop = asyncio.get_running_loop()
    _dispatch_task = loop.create_task(_dispatch_loop())
    logger.info("event stream dispatcher started")


async def stop_dispatcher() -> None:
    """
    イベント配信タスクを停止する。

    アプリケーション終了時に呼び出してタスクをキャンセルする。
    """
    global _dispatch_task
    if _dispatch_task is None:
        return
    _dispatch_task.cancel()
    try:
        await _dispatch_task
    except asyncio.CancelledError:  # pragma: no cover
        pass
    _dispatch_task = None


def publish(*, type: str, embedding_preset_id: str, unit_id: int, data: Optional[Dict[str, Any]] = None) -> None:
    """
    イベントをキューに投入する。

    スレッドセーフにイベントを追加し、dispatcherが配信を行う。
    """
    if _event_queue is None or _loop is None:
        return
    event = AppEvent(
        event_id=str(uuid.uuid4()),
        ts=datetime.now(timezone.utc).isoformat(),
        type=type,
        embedding_preset_id=embedding_preset_id,
        unit_id=int(unit_id),
        data=data or {},
    )
    _loop.call_soon_threadsafe(_event_queue.put_nowait, event)


def get_buffer_snapshot() -> List[AppEvent]:
    """
    バッファ内の直近イベントを取得する。

    新規接続時のキャッチアップ用にリングバッファの内容を返す。
    """
    return list(_buffer)


async def add_client(ws: "WebSocket") -> None:
    """
    WebSocketクライアントを購読リストに登録する。

    以降のイベントがこのクライアントに配信される。
    """
    _clients.add(ws)


async def remove_client(ws: "WebSocket") -> None:
    """
    WebSocketクライアントを購読リストから解除する。

    切断時やエラー時に呼び出される。
    """
    _clients.discard(ws)


async def send_buffer(ws: "WebSocket") -> None:
    """
    バッファ内のイベントを送信する。

    新規接続時にキャッチアップとして直近イベントを順に送信する。
    """
    for event in get_buffer_snapshot():
        await ws.send_text(_serialize_event(event))


async def _dispatch_loop() -> None:
    while True:
        if _event_queue is None:  # pragma: no cover
            await asyncio.sleep(0.1)
            continue
        event = await _event_queue.get()
        _buffer.append(event)
        payload = _serialize_event(event)

        dead_clients: List["WebSocket"] = []
        for ws in list(_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead_clients.append(ws)

        for ws in dead_clients:
            await remove_client(ws)
