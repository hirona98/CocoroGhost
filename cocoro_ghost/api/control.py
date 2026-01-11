"""
/control エンドポイント

プロセス自体の制御（終了要求など）を受け付ける。
本APIは CocoroConsole 等の管理UIから呼び出される想定で、Bearer 認証必須とする。
"""

from __future__ import annotations

import os
import signal
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Response, status

from cocoro_ghost import schemas

logger = __import__("logging").getLogger(__name__)

router = APIRouter()


def _request_process_shutdown(*, reason: Optional[str]) -> None:
    """
    CocoroGhost プロセスの終了を要求する。

    FastAPI のハンドラ内で即座に終了させると、HTTP レスポンスの返却が不安定になるため、
    BackgroundTask として少し遅延させてから SIGTERM を送る。
    """

    # --- ログ出力（UI 操作の追跡用） ---
    logger.warning("shutdown requested", extra={"reason": (reason or "").strip()})

    # --- 先にレスポンスを返しやすくするための短い遅延 ---
    time.sleep(0.2)

    # --- uvicorn に停止シグナルを送る（shutdown イベントが走る） ---
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/control", status_code=status.HTTP_204_NO_CONTENT)
def control(
    request: schemas.ControlRequest,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    プロセス制御コマンドを受け付ける。

    現状は shutdown のみをサポートし、受理後にプロセス終了を要求する。
    """

    # --- action バリデーションは schemas 側で実施済み ---
    background_tasks.add_task(_request_process_shutdown, reason=request.reason)
    # --- BackgroundTasks を紐づける（これが無いと shutdown が実行されない） ---
    return Response(status_code=status.HTTP_204_NO_CONTENT, background=background_tasks)
