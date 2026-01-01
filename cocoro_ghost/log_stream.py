"""
WebSocket向けログ配信サポート

アプリケーションログをWebSocketクライアントにリアルタイム配信する。
ログはリングバッファに保持され、新規接続時に直近のログを送信する。
"""

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
    """
    配信用のログイベント。

    ログレコードをシリアライズ可能な形式で保持する。
    """

    ts: str  # ISO形式タイムスタンプ
    level: str  # ログレベル（INFO/WARNING等）
    logger: str  # ロガー名
    msg: str  # メッセージ


_log_queue: Optional[asyncio.Queue[LogEvent]] = None
_buffer: Deque[LogEvent] = deque(maxlen=MAX_BUFFER)
_clients: Set["WebSocket"] = set()
_dispatch_task: Optional[asyncio.Task[None]] = None
_handler_installed = False
_installed_handler: Optional[logging.Handler] = None
_attached_logger_names: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    # LLM I/O loggers are propagate=False, so attach explicitly.
    "cocoro_ghost.llm_io.console",
)
logger = logging.getLogger(__name__)


def _serialize_event(event: LogEvent) -> str:
    """
    ログイベントをJSON文字列にシリアライズする。

    改行文字はスペースに置換してWebSocket送信に適した形式にする。
    """
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
    """
    loggingからasyncio.Queueへのブリッジハンドラ。

    標準ログをasyncioキューに転送し、WebSocket配信を可能にする。
    """

    def __init__(self, queue: asyncio.Queue[LogEvent], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.queue = queue
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - simple passthrough
        """
        ログレコードをイベントに変換してキューに投入する。

        イベントループ終了時やログストリーム関連のログは無視する。
        """
        try:
            # shutdown レース対策:
            # サーバ停止時にイベントループが先に閉じられると call_soon_threadsafe が例外になる。
            # ここで logger.exception すると同じハンドラを経由して再帰するので、黙ってドロップする。
            if self.loop.is_closed():
                return

            # /api/logs/stream に関するアクセスログは配信しない
            msg = record.getMessage()
            if "logs/stream" in msg:
                return
            event = _record_to_event(record)
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except Exception:  # pragma: no cover - logging safety net
            # ここで例外ログを出すと、同じハンドラ経由で再帰する可能性があるため抑止する。
            return


def install_log_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
    ログストリーム用ハンドラを設置する。

    ルートロガーとuvicorn関連ロガーにQueueHandlerを追加する。
    多重呼び出しは無視される。
    """
    global _log_queue, _handler_installed, _installed_handler
    if _handler_installed:
        return

    _log_queue = asyncio.Queue()
    handler = _QueueHandler(_log_queue, loop)
    handler.setLevel(logging.getLogger().level)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    # propagate=False のロガーは個別にハンドラを付与する
    for name in _attached_logger_names:
        logging.getLogger(name).addHandler(handler)

    _installed_handler = handler
    _handler_installed = True
    logger.info("log stream handler installed")


async def start_dispatcher() -> None:
    """
    ログ配信タスクを起動する。

    キューからログを取り出し、接続中の全クライアントへ配信する。
    """
    global _dispatch_task
    if _dispatch_task is not None:
        return
    if _log_queue is None:
        raise RuntimeError("log queue is not initialized. call install_log_handler() first.")

    loop = asyncio.get_running_loop()
    _dispatch_task = loop.create_task(_dispatch_loop())
    logger.info("log stream dispatcher started")


async def stop_dispatcher() -> None:
    """
    ログ配信タスクを停止する。

    タスクをキャンセルし、ハンドラをロガーから解除する。
    """
    global _dispatch_task, _handler_installed, _installed_handler
    if _dispatch_task is not None:
        _dispatch_task.cancel()
        try:
            await _dispatch_task
        except asyncio.CancelledError:  # pragma: no cover - expected on cancel
            pass
        _dispatch_task = None

    if not _handler_installed or _installed_handler is None:
        return

    handler = _installed_handler
    root_logger = logging.getLogger()
    root_logger.removeHandler(handler)

    # propagate=False のロガーも個別に解除する
    for name in _attached_logger_names:
        logging.getLogger(name).removeHandler(handler)

    _installed_handler = None
    _handler_installed = False


def get_buffer_snapshot() -> List[LogEvent]:
    """
    バッファ内のログイベントを取得する。

    新規接続時のキャッチアップ用にリングバッファの内容を返す。
    """
    return list(_buffer)


async def add_client(ws: "WebSocket") -> None:
    """
    WebSocketクライアントを購読リストに登録する。

    以降のログがこのクライアントに配信される。
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
    バッファ内のログを送信する。

    新規接続時にキャッチアップとして直近500件のログを送信する。
    """
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
