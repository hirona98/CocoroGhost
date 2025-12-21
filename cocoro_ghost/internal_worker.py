"""FastAPIプロセス内で動く内蔵Workerの管理。"""

from __future__ import annotations

import threading

from cocoro_ghost.worker import run_forever


_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


def is_alive() -> bool:
    """内蔵Workerスレッドが稼働中か返す。"""
    t = _thread
    return t is not None and t.is_alive()


def start(*, memory_id: str, embedding_dimension: int) -> None:
    """内蔵Workerスレッドを起動する（起動済みなら何もしない）。"""
    from cocoro_ghost.config import get_config_store
    from cocoro_ghost.deps import get_llm_client

    if not get_config_store().memory_enabled:
        return

    with _lock:
        global _thread, _stop_event
        if _thread is not None and _thread.is_alive():
            return

        stop_event = threading.Event()
        llm_client = get_llm_client()

        t = threading.Thread(
            target=run_forever,
            kwargs={
                "memory_id": str(memory_id),
                "embedding_dimension": int(embedding_dimension),
                "llm_client": llm_client,
                # 固定値（cron無し定期enqueue込み）
                "poll_interval_seconds": 1.0,
                "max_jobs_per_tick": 10,
                "periodic_interval_seconds": 30.0,
                "stop_event": stop_event,
            },
            name="cocoro_ghost_internal_worker",
            daemon=True,
        )
        _stop_event = stop_event
        _thread = t
        t.start()


def stop(*, timeout_seconds: float = 5.0) -> None:
    """内蔵Workerスレッドに停止を通知し、指定秒数までjoinする。"""
    with _lock:
        global _thread, _stop_event
        if _stop_event is not None:
            _stop_event.set()
        t = _thread

    if t is not None:
        t.join(timeout_seconds)

    with _lock:
        _thread = None
        _stop_event = None


def restart(*, memory_id: str, embedding_dimension: int) -> None:
    """設定変更後に内蔵Workerを追従させるための再起動。"""
    stop()
    start(memory_id=memory_id, embedding_dimension=embedding_dimension)


def request_restart_async(*, memory_id: str, embedding_dimension: int) -> None:
    """BackgroundTasks等から呼ぶ用（例外を外に出さない）。"""
    try:
        restart(memory_id=memory_id, embedding_dimension=embedding_dimension)
    except Exception:  # noqa: BLE001
        # ここでログ依存を持たせない（呼び出し側でログする）
        return
