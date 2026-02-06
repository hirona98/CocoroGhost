"""
WebSocket向けアプリイベント配信

通知完了、メタ要求完了などのアプリケーションイベントを
WebSocketクライアントにリアルタイム配信する。

Planned:
- 視覚（Vision）のための命令（capture_request）を同じストリームで配信する。
- 命令は特定クライアント（client_id）宛てに送る（broadcastしない）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket


@dataclass
class AppEvent:
    """
    WebSocket配信用のイベント。

    - `event_id` は出来事ログ（events）のIDと一致させる
    - 保存しない命令（例: vision.capture_request）は event_id=0 とする
    """

    type: str  # イベント種別（notification_done, meta_done等）
    event_id: int  # 関連Event ID（events.event_id）
    data: Dict[str, Any]  # 追加データ
    target_client_id: Optional[str] = None  # 宛先client_id（指定時はそのクライアントにのみ送る）


_event_queue: Optional[asyncio.Queue[AppEvent]] = None
_clients: Set["WebSocket"] = set()
_ws_to_client_id: dict["WebSocket", str] = {}
_client_id_to_ws: dict[str, "WebSocket"] = {}
_ws_to_caps: dict["WebSocket", list[str]] = {}
_dispatch_task: Optional[asyncio.Task[None]] = None
_handler_installed = False
_loop: Optional[asyncio.AbstractEventLoop] = None
logger = logging.getLogger(__name__)

# --- 配信バックプレッシャー設定 ---
# NOTE:
# - キューは有界にして、遅延時のメモリ膨張を防ぐ。
# - 送信はタイムアウトを設け、遅いクライアントを切り離す。
_EVENT_QUEUE_MAXSIZE = 1000
_SEND_TIMEOUT_SECONDS = 2.0


def _serialize_event(event: AppEvent) -> str:
    """
    イベントをJSON文字列にシリアライズする。

    WebSocket送信用の最小ペイロードに整形する。
    """
    return json.dumps(
        {
            "event_id": int(event.event_id),
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
    _event_queue = asyncio.Queue(maxsize=int(_EVENT_QUEUE_MAXSIZE))
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


def _enqueue_event_nonblocking(event: AppEvent) -> None:
    """イベントを non-blocking でキュー投入する（満杯時はドロップ）。"""

    if _event_queue is None:
        return
    try:
        _event_queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "event stream queue full; dropped type=%s event_id=%s",
            str(event.type or ""),
            int(event.event_id),
        )


def publish(
    *,
    type: str,
    event_id: int,
    data: Optional[Dict[str, Any]] = None,
    target_client_id: Optional[str] = None,
) -> None:
    """
    イベントをキューに投入する。

    スレッドセーフにイベントを追加し、dispatcherが配信を行う。
    target_client_id を指定した場合は、そのクライアントにのみ送る。
    """
    if _event_queue is None or _loop is None:
        return
    event = AppEvent(
        type=type,
        event_id=int(event_id),
        data=data or {},
        target_client_id=(str(target_client_id).strip() if target_client_id else None),
    )
    try:
        _loop.call_soon_threadsafe(_enqueue_event_nonblocking, event)
    except RuntimeError:
        # --- shutdown レース（loop close 後）は捨てる ---
        return


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

    # --- client_id 登録情報を掃除する ---
    client_id = _ws_to_client_id.pop(ws, None)
    _ws_to_caps.pop(ws, None)
    if client_id and _client_id_to_ws.get(client_id) is ws:
        _client_id_to_ws.pop(client_id, None)


def register_client_identity(ws: "WebSocket", *, client_id: str, caps: Optional[list[str]] = None) -> None:
    """
    WebSocket接続に client_id を紐づける。

    クライアント（CocoroConsole等）が hello メッセージで自己申告した情報を保持し、
    視覚（Vision）命令の宛先指定に利用する。
    """
    cid = str(client_id or "").strip()
    if not cid:
        return

    # --- 既存の紐づけを更新する ---
    old = _ws_to_client_id.get(ws)
    if old and _client_id_to_ws.get(old) is ws:
        _client_id_to_ws.pop(old, None)

    _ws_to_client_id[ws] = cid
    _client_id_to_ws[cid] = ws
    _ws_to_caps[ws] = list(caps or [])
    logger.info("event client registered client_id=%s caps=%s", cid, list(caps or []))


def is_client_connected(client_id: str) -> bool:
    """
    指定 client_id のクライアントが接続中かを返す。

    視覚（Vision）要求の送信前チェックなどに利用する。
    """
    cid = str(client_id or "").strip()
    if not cid:
        return False
    ws = _client_id_to_ws.get(cid)
    return bool(ws is not None and ws in _clients)


def get_connected_client_count() -> int:
    """
    接続中クライアント数を返す。

    リマインダー等の「ブロードキャスト前提」の機能で、
    「接続0のときは due を保持する」判定に利用する。
    """

    # NOTE:
    # - _clients はイベントループ側で更新されるが、他スレッドから参照される用途もある。
    # - ここでは「概数が取れれば十分」なので、ロックは導入しない。
    return int(len(_clients))


def has_any_client_connected() -> bool:
    """
    接続中クライアントが1つ以上あるかを返す。

    例:
    - リマインダー: 接続0なら発火せず保持する
    """

    return get_connected_client_count() > 0


async def _dispatch_loop() -> None:
    while True:
        if _event_queue is None:  # pragma: no cover
            await asyncio.sleep(0.1)
            continue
        event = await _event_queue.get()
        payload = _serialize_event(event)

        dead_clients: List["WebSocket"] = []

        # --- 配信 ---
        # 宛先指定があれば、その client_id のみへ送る。
        target_id = (event.target_client_id or "").strip()
        if target_id:
            ws = _client_id_to_ws.get(target_id)
            # --- 送信ログ（通常イベント/命令 共通） ---
            # NOTE: 送信ペイロード（dataの中身）はログに出さず、type/event_id/宛先だけを記録する。
            if ws is not None and ws in _clients:
                logger.info(
                    "event stream send type=%s event_id=%s target_client_id=%s",
                    event.type,
                    int(event.event_id),
                    target_id,
                )
            else:
                logger.info(
                    "event stream send skipped (target not connected) type=%s event_id=%s target_client_id=%s",
                    event.type,
                    int(event.event_id),
                    target_id,
                )
            if ws is not None and ws in _clients:
                try:
                    await asyncio.wait_for(ws.send_text(payload), timeout=float(_SEND_TIMEOUT_SECONDS))
                except Exception:
                    dead_clients.append(ws)
        else:
            # --- 送信ログ（ブロードキャスト） ---
            # NOTE: ブロードキャストの場合は宛先が複数になり得るため、接続数のみ記録する。
            logger.info(
                "event stream broadcast type=%s event_id=%s clients=%s",
                event.type,
                int(event.event_id),
                len(_clients),
            )
            for ws in list(_clients):
                try:
                    await asyncio.wait_for(ws.send_text(payload), timeout=float(_SEND_TIMEOUT_SECONDS))
                except Exception:
                    dead_clients.append(ws)

        for ws in dead_clients:
            await remove_client(ws)
