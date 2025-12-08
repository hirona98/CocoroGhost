"""/meta_request エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_db_dep, get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/meta_request", response_model=schemas.MetaRequestResponse)
def meta_request(
    request: schemas.MetaRequestRequest,
    db: Session = Depends(get_memory_db_dep),
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """メタリクエストを処理してエピソードを作成。"""
    return memory_manager.handle_meta_request(db, request)
