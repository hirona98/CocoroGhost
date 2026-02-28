"""
定期実行タスク（periodic task）ユーティリティ

FastAPI の startup/shutdown に合わせて、一定間隔で実行する asyncio タスクを管理する。

目的:
- fastapi-utils に依存せず、標準 asyncio だけで「毎N秒」を実現する
- 例外が起きてもタスクが死なず、ログに残して継続する
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


_TASKS_STATE_KEY = "_cocoro_periodic_tasks"


def start_periodic_task(
    app: "FastAPI",
    *,
    name: str,
    interval_seconds: float,
    wait_first: bool,
    func: Callable[[], Awaitable[None]],
    logger: logging.Logger,
) -> asyncio.Task[None]:
    """
    定期実行タスクを開始して FastAPI app.state に登録する。

    Args:
        app: FastAPI アプリ。
        name: asyncio タスク名（デバッグ用）。
        interval_seconds: 実行間隔（秒）。
        wait_first: True の場合、最初の実行前に interval だけ待つ。
        func: 1回分の処理（awaitable）。
        logger: 例外ログ出力に使用するロガー。

    Returns:
        作成した asyncio.Task。
    """
    # --- app.state にタスクリストを確保する ---
    tasks = getattr(app.state, _TASKS_STATE_KEY, None)
    if tasks is None:
        setattr(app.state, _TASKS_STATE_KEY, [])
        tasks = getattr(app.state, _TASKS_STATE_KEY)

    # --- 定期実行ループ ---
    async def _runner() -> None:
        # --- 初回待機（fastapi-utils の wait_first 相当） ---
        if wait_first:
            await asyncio.sleep(float(interval_seconds))

        # --- キャンセルされるまで定期実行 ---
        while True:
            try:
                await func()
            except asyncio.CancelledError:
                # --- shutdown で cancel されたら素直に終了 ---
                raise
            except Exception as exc:  # noqa: BLE001
                # --- 例外は落とさずに記録して継続 ---
                logger.exception("periodic task failed: name=%s error=%s", name, str(exc))

            await asyncio.sleep(float(interval_seconds))

    task = asyncio.create_task(_runner(), name=str(name))
    tasks.append(task)
    return task


async def stop_periodic_tasks(app: "FastAPI", *, logger: logging.Logger) -> None:
    """
    app.state に登録された定期実行タスクを停止する。

    Args:
        app: FastAPI アプリ。
        logger: 停止処理のログ出力に使用するロガー。
    """
    # --- 登録済みタスクを取り出す（無ければ何もしない） ---
    tasks: list[asyncio.Task[None]] | None = getattr(app.state, _TASKS_STATE_KEY, None)
    if not tasks:
        return

    # --- 先に cancel して一斉停止 ---
    for t in list(tasks):
        try:
            t.cancel()
        except Exception:  # pragma: no cover
            continue

    # --- gather して終了を待つ（CancelledError は想定内） ---
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # --- 状態を初期化して二重停止を避ける ---
        setattr(app.state, _TASKS_STATE_KEY, [])
        logger.info("periodic tasks stopped")
