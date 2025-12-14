"""/capture エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/capture", response_model=schemas.CaptureResponse)
def capture(
    request: schemas.CaptureRequest,
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """画面/カメラキャプチャをUnit(Episode)として保存し、派生ジョブを積む。"""
    return memory_manager.handle_capture(request)

