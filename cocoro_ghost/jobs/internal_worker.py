"""
FastAPIプロセス内で動く内蔵Workerの管理

アプリケーション起動時にバックグラウンドスレッドでWorkerを起動し、
非同期ジョブ（WritePlan、状態/感情/文脈更新、埋め込み生成等）を処理する。
設定変更時には自動で再起動される。
"""

from __future__ import annotations

import threading

from cocoro_ghost.jobs.runner import run_forever


_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
logger = __import__("logging").getLogger(__name__)


def is_alive() -> bool:
    """
    内蔵Workerスレッドの稼働状態を確認する。

    スレッドが存在し、実行中であればTrueを返す。
    """
    t = _thread
    return t is not None and t.is_alive()


def start(*, embedding_preset_id: str, embedding_dimension: int) -> None:
    """
    内蔵Workerスレッドを起動する。

    既に起動済みの場合や記憶機能が無効な場合は何もしない。
    デーモンスレッドとして起動し、アプリ終了時に自動停止する。
    """
    from cocoro_ghost.config import get_config_store
    from cocoro_ghost.app_bootstrap.dependencies import get_llm_client

    # --- 記憶が無効なら、Workerも動かない（誤解を避けるためログする） ---
    if not get_config_store().memory_enabled:
        logger.info("internal worker skipped (memory_enabled=false)")
        return

    with _lock:
        global _thread, _stop_event
        if _thread is not None and _thread.is_alive():
            logger.info("internal worker already running")
            return

        stop_event = threading.Event()
        llm_client = get_llm_client()

        t = threading.Thread(
            target=run_forever,
            kwargs={
                "embedding_preset_id": str(embedding_preset_id),
                "embedding_dimension": int(embedding_dimension),
                "llm_client": llm_client,
                # 固定値（cron無し定期enqueue込み）
                "poll_interval_seconds": 1.0,
                "max_jobs_per_tick": 10,
                "periodic_interval_seconds": 30.0,
                # ジョブ実行の並列数（LLM待ちを隠す目的。sqliteロック競合を増やし過ぎない範囲に固定）
                "job_concurrency": 4,
                "stop_event": stop_event,
            },
            name="cocoro_ghost_internal_worker",
            daemon=True,
        )
        _stop_event = stop_event
        _thread = t
        t.start()
        logger.info("internal worker thread started")


def stop(*, timeout_seconds: float = 5.0) -> None:
    """
    内蔵Workerスレッドを停止する。

    停止イベントをセットし、指定秒数までスレッドの終了を待機する。
    """
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


def restart(*, embedding_preset_id: str, embedding_dimension: int) -> None:
    """
    内蔵Workerを再起動する。

    設定変更後にWorkerを新しい設定で再起動させる。
    """
    stop()
    start(embedding_preset_id=embedding_preset_id, embedding_dimension=embedding_dimension)


def request_restart_async(*, embedding_preset_id: str, embedding_dimension: int) -> None:
    """
    非同期コンテキストからWorkerを再起動する。

    BackgroundTasks等から呼び出され、例外を外部に伝播させない。
    """
    try:
        restart(embedding_preset_id=embedding_preset_id, embedding_dimension=embedding_dimension)
    except Exception:  # noqa: BLE001
        # ここでログ依存を持たせない（呼び出し側でログする）
        return
