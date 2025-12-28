"""/v1/notification エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/v1/notification", status_code=status.HTTP_204_NO_CONTENT)
def notification_v1(
    request: schemas.NotificationV1Request,
    background_tasks: BackgroundTasks,
    memory_manager: MemoryManager = Depends(get_memory_manager),
) -> Response:
    """通知をUnit(Episode)として保存し、派生ジョブを積む。"""
    images = [{"type": "data_uri", "base64": schemas.data_uri_image_to_base64(s)} for s in request.images]
    internal = schemas.NotificationRequest(source_system=request.source_system, text=request.text, images=images)
    memory_manager.handle_notification(internal, background_tasks=background_tasks)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
