"""/chat エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_db_dep, get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/chat")
def chat(
    request: schemas.ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_memory_db_dep),
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """チャットリクエストを SSE ストリーミングで返却。"""
    generator = memory_manager.stream_chat(db, request, background_tasks)
    return StreamingResponse(generator, media_type="text/event-stream")
