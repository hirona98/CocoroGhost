"""/notification エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/notification", response_model=schemas.NotificationResponse)
def notification(
    request: schemas.NotificationRequest,
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """通知をUnit(Episode)として保存し、派生ジョブを積む。"""
    return memory_manager.handle_notification(request)

