"""
/v2/notification エンドポイント

外部システム（ファイル監視、カレンダー、RSSリーダー等）からの通知を受け付ける。

注意:
- BackgroundTasks は Response に紐づけないと実行されない。
  本エンドポイントは 204 を返すため、Response(background=...) を明示する。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/v2/notification", status_code=status.HTTP_204_NO_CONTENT)
def notification_v2(
    request: schemas.NotificationV2Request,
    background_tasks: BackgroundTasks,
    memory_manager: MemoryManager = Depends(get_memory_manager),
) -> Response:
    """通知をUnit(Episode)として保存し、派生ジョブを積む。"""
    internal = schemas.NotificationRequest(
        client_id=request.client_id,
        source_system=request.source_system,
        text=request.text,
        images=list(request.images or []),
    )
    memory_manager.handle_notification(internal, background_tasks=background_tasks)
    # --- BackgroundTasks を紐づける（これが無いと enqueue が実行されない） ---
    return Response(status_code=status.HTTP_204_NO_CONTENT, background=background_tasks)
