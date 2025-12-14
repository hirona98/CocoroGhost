"""WebSocket向けのアプリイベント配信サポート（通知/メタ等）。"""

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
    event_id: str
    ts: str
    type: str
    memory_id: str
    unit_id: int
    data: Dict[str, Any]


_event_queue: Optional[asyncio.Queue[AppEvent]] = None
_buffer: Deque[AppEvent] = deque(maxlen=MAX_BUFFER)
_clients: Set["WebSocket"] = set()
_dispatch_task: Optional[asyncio.Task[None]] = None
_handler_installed = False
_loop: Optional[asyncio.AbstractEventLoop] = None
logger = logging.getLogger(__name__)


def _serialize_event(event: AppEvent) -> str:
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
    """publish() のためにイベントループとキューを初期化。多重実行はしない。"""
    global _event_queue, _handler_installed, _loop
    if _handler_installed:
        return
    _loop = loop
    _event_queue = asyncio.Queue()
    _handler_installed = True
    logger.info("event stream installed")


async def start_dispatcher() -> None:
    global _dispatch_task
    if _dispatch_task is not None:
        return
    if _event_queue is None:
        raise RuntimeError("event queue is not initialized. call install() first.")
    loop = asyncio.get_running_loop()
    _dispatch_task = loop.create_task(_dispatch_loop())
    logger.info("event stream dispatcher started")


async def stop_dispatcher() -> None:
    global _dispatch_task
    if _dispatch_task is None:
        return
    _dispatch_task.cancel()
    try:
        await _dispatch_task
    except asyncio.CancelledError:  # pragma: no cover
        pass
    _dispatch_task = None


def publish(*, type: str, memory_id: str, unit_id: int, data: Optional[Dict[str, Any]] = None) -> None:
    """スレッドセーフにイベントを投入（WS配送はdispatcherが行う）。"""
    if _event_queue is None or _loop is None:
        return
    event = AppEvent(
        event_id=str(uuid.uuid4()),
        ts=datetime.now(timezone.utc).isoformat(),
        type=type,
        memory_id=memory_id,
        unit_id=int(unit_id),
        data=data or {},
    )
    _loop.call_soon_threadsafe(_event_queue.put_nowait, event)


def get_buffer_snapshot() -> List[AppEvent]:
    return list(_buffer)


async def add_client(ws: "WebSocket") -> None:
    _clients.add(ws)


async def remove_client(ws: "WebSocket") -> None:
    _clients.discard(ws)


async def send_buffer(ws: "WebSocket") -> None:
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
