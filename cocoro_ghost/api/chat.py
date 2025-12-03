"""/chat エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_db_dep, get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/chat", response_model=schemas.ChatResponse)
async def chat(
    request: schemas.ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_memory_db_dep),
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """チャットリクエストを処理。"""
    return memory_manager.handle_chat(db, request, background_tasks)
