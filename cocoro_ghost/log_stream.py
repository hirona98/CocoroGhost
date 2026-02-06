"""
WebSocket向けログ配信サポート

アプリケーションログをWebSocketクライアントにリアルタイム配信する。
ログはリングバッファに保持され、新規接続時に直近のログを送信する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket


MAX_BUFFER = 500
# --- 配信バックプレッシャー設定 ---
# NOTE:
# - キューは有界にして、遅延時のメモリ膨張を防ぐ。
# - 送信はタイムアウトを設け、遅いクライアントを切り離す。
_LOG_QUEUE_MAXSIZE = 1000
_SEND_TIMEOUT_SECONDS = 2.0


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

# --- ランタイム統計 ---
# NOTE:
# - API から drop/送信失敗を観測できるように軽量カウンタを保持する。
# - emit は複数スレッドから呼ばれ得るため lock で保護する。
_stats_lock = threading.Lock()
_stats: dict[str, int] = {
    "enqueued_total": 0,
    "dropped_queue_full_total": 0,
    "send_ok_total": 0,
    "send_error_total": 0,
    "emit_skipped_loop_closed_total": 0,
    "emit_error_total": 0,
}


def _reset_runtime_stats() -> None:
    """ログストリーム統計を初期化する。"""

    # --- すべての統計値を0に戻す ---
    with _stats_lock:
        for key in list(_stats.keys()):
            _stats[key] = 0


def _increment_stat(key: str, delta: int = 1) -> None:
    """指定した統計カウンタを加算する。"""

    # --- 未知キーは作らず無視する（呼び出し側バグ時の二次障害を防ぐ） ---
    with _stats_lock:
        if key in _stats:
            _stats[key] = int(_stats[key]) + int(delta)


def _snapshot_stats() -> dict[str, int]:
    """統計カウンタのスナップショットを返す。"""

    with _stats_lock:
        return {k: int(v) for k, v in _stats.items()}


def get_runtime_stats() -> dict[str, int | bool]:
    """
    ログストリームのランタイム統計を返す。

    運用時の観測（queue逼迫/ドロップ/送信失敗）に利用する。
    """

    # --- queue/buffer の現在値を取得する ---
    queue = _log_queue
    queue_size = int(queue.qsize()) if queue is not None else 0
    queue_maxsize = int(getattr(queue, "maxsize", 0) or 0) if queue is not None else int(_LOG_QUEUE_MAXSIZE)
    buffer_size = int(len(_buffer))
    buffer_max = int(getattr(_buffer, "maxlen", MAX_BUFFER) or MAX_BUFFER)

    # --- 統計カウンタをコピーする ---
    counters = _snapshot_stats()

    # --- API応答向けに整形する ---
    return {
        "connected_clients": int(len(_clients)),
        "queue_size": int(queue_size),
        "queue_maxsize": int(queue_maxsize),
        "buffer_size": int(buffer_size),
        "buffer_max": int(buffer_max),
        "dispatcher_running": bool(_dispatch_task is not None and not _dispatch_task.done()),
        "enqueued_total": int(counters.get("enqueued_total", 0)),
        "dropped_queue_full_total": int(counters.get("dropped_queue_full_total", 0)),
        "send_ok_total": int(counters.get("send_ok_total", 0)),
        "send_error_total": int(counters.get("send_error_total", 0)),
        "emit_skipped_loop_closed_total": int(counters.get("emit_skipped_loop_closed_total", 0)),
        "emit_error_total": int(counters.get("emit_error_total", 0)),
    }


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
                _increment_stat("emit_skipped_loop_closed_total", 1)
                return

            # /api/logs/stream に関するアクセスログは配信しない
            msg = record.getMessage()
            if "logs/stream" in msg:
                return
            event = _record_to_event(record)
            self.loop.call_soon_threadsafe(_enqueue_log_nonblocking, self.queue, event)
        except Exception:  # pragma: no cover - logging safety net
            # ここで例外ログを出すと、同じハンドラ経由で再帰する可能性があるため抑止する。
            _increment_stat("emit_error_total", 1)
            return


def _enqueue_log_nonblocking(queue: asyncio.Queue[LogEvent], event: LogEvent) -> None:
    """
    ログイベントをnon-blockingでキュー投入する。

    queue満杯時はドロップする。ここでログ出力すると再帰するため出力しない。
    """

    # --- queue満杯時は黙ってドロップする ---
    # NOTE:
    # - ここで logger.warning を出すと同じhandlerを経由して再帰する。
    # - ログ配信はベストエフォートとし、アプリ本体の処理を優先する。
    try:
        queue.put_nowait(event)
        _increment_stat("enqueued_total", 1)
    except asyncio.QueueFull:
        _increment_stat("dropped_queue_full_total", 1)
        return


def install_log_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
    ログストリーム用ハンドラを設置する。

    ルートロガーにQueueHandlerを追加する。
    propagate=False のロガーのみ個別に追加して二重配信を避ける。
    多重呼び出しは無視される。
    """
    global _log_queue, _handler_installed, _installed_handler
    if _handler_installed:
        return

    _log_queue = asyncio.Queue(maxsize=int(_LOG_QUEUE_MAXSIZE))
    _reset_runtime_stats()
    handler = _QueueHandler(_log_queue, loop)
    handler.setLevel(logging.getLogger().level)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    # propagate=True の場合は root 経由で届くため、二重配信を避けて付与しない
    # propagate=False のロガーだけ個別にハンドラを付与する
    for name in _attached_logger_names:
        target_logger = logging.getLogger(name)
        if target_logger.propagate:
            continue
        if handler in target_logger.handlers:
            continue
        target_logger.addHandler(handler)

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
        try:
            await asyncio.wait_for(ws.send_text(_serialize_event(event)), timeout=float(_SEND_TIMEOUT_SECONDS))
            _increment_stat("send_ok_total", 1)
        except Exception:
            _increment_stat("send_error_total", 1)
            raise


async def _dispatch_loop() -> None:
    while True:
        if _log_queue is None:  # pragma: no cover
            await asyncio.sleep(0.1)
            continue
        event = await _log_queue.get()
        _buffer.append(event)
        dead_clients: List["WebSocket"] = []
        payload = _serialize_event(event)

        for ws in list(_clients):
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=float(_SEND_TIMEOUT_SECONDS))
                _increment_stat("send_ok_total", 1)
            except Exception:
                _increment_stat("send_error_total", 1)
                dead_clients.append(ws)

        for ws in dead_clients:
            await remove_client(ws)
