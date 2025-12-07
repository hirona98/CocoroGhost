"""WebSocket向けのログ配信サポート。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket


MAX_BUFFER = 500


@dataclass
class LogEvent:
    """配信用のログイベント。"""

    ts: str
    level: str
    logger: str
    msg: str


_log_queue: Optional[asyncio.Queue[LogEvent]] = None
_buffer: Deque[LogEvent] = deque(maxlen=MAX_BUFFER)
_clients: Set["WebSocket"] = set()
_dispatch_task: Optional[asyncio.Task[None]] = None
_handler_installed = False
logger = logging.getLogger(__name__)


def _serialize_event(event: LogEvent) -> str:
    """JSON文字列に整形（改行はスペースに潰す）。"""
    clean_msg = event.msg.replace("\r", " ").replace("\n", " ")
    return json.dumps(
        {
            "ts": event.ts,
            "level": event.level,
            "logger": event.logger,
            "msg": clean_msg,
        },
        ensure_ascii=False,
    )


def _record_to_event(record: logging.LogRecord) -> LogEvent:
    ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
    msg = record.getMessage()
    return LogEvent(ts=ts, level=record.levelname, logger=record.name, msg=msg)


class _QueueHandler(logging.Handler):
    """logging -> asyncio.Queue ブリッジ。"""

    def __init__(self, queue: asyncio.Queue[LogEvent], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.queue = queue
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - simple passthrough
        try:
            event = _record_to_event(record)
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except Exception:  # pragma: no cover - logging safety net
            logger.exception("failed to enqueue log record")


def install_log_handler(loop: asyncio.AbstractEventLoop) -> None:
    """ルートロガーにQueueHandlerを追加。多重追加はしない。"""
    global _log_queue, _handler_installed
    if _handler_installed:
        return

    _log_queue = asyncio.Queue()
    handler = _QueueHandler(_log_queue, loop)
    handler.setLevel(logging.getLogger().level)
    logging.getLogger().addHandler(handler)
    _handler_installed = True
    logger.info("log stream handler installed")


async def start_dispatcher() -> None:
    """ログ配送タスクを起動（既に動作中なら何もしない）。"""
    global _dispatch_task
    if _dispatch_task is not None:
        return
    if _log_queue is None:
        raise RuntimeError("log queue is not initialized. call install_log_handler() first.")

    loop = asyncio.get_running_loop()
    _dispatch_task = loop.create_task(_dispatch_loop())
    logger.info("log stream dispatcher started")


async def stop_dispatcher() -> None:
    """配送タスクを停止。"""
    global _dispatch_task
    if _dispatch_task is None:
        return
    _dispatch_task.cancel()
    try:
        await _dispatch_task
    except asyncio.CancelledError:  # pragma: no cover - expected on cancel
        pass
    _dispatch_task = None


def get_buffer_snapshot() -> List[LogEvent]:
    """リングバッファのスナップショットを返す。"""
    return list(_buffer)


async def add_client(ws: "WebSocket") -> None:
    _clients.add(ws)


async def remove_client(ws: "WebSocket") -> None:
    _clients.discard(ws)


async def send_buffer(ws: "WebSocket") -> None:
    """接続直後に直近500件を送信。"""
    for event in get_buffer_snapshot():
        await ws.send_text(_serialize_event(event))


async def _dispatch_loop() -> None:
    while True:
        event = await _log_queue.get()
        _buffer.append(event)
        dead_clients: List["WebSocket"] = []
        payload = _serialize_event(event)

        for ws in list(_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead_clients.append(ws)

        for ws in dead_clients:
            await remove_client(ws)
