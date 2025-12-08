"""/capture エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_db_dep, get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/capture", response_model=schemas.CaptureResponse)
def capture(
    request: schemas.CaptureRequest,
    db: Session = Depends(get_memory_db_dep),
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """画面/カメラキャプチャを処理してエピソードを作成。"""
    return memory_manager.handle_capture(db, request)
