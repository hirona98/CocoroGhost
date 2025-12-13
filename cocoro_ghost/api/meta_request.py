"""/meta_request エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/meta_request", response_model=schemas.MetaRequestResponse)
def meta_request(
    request: schemas.MetaRequestRequest,
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """メタ要求をUnit(Episode)として保存し、派生ジョブを積む。"""
    return memory_manager.handle_meta_request(request)

