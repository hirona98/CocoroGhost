"""
/chat エンドポイント

ユーザーからのチャットリクエストを受け付け、LLMの応答をSSE（Server-Sent Events）
形式でストリーミング返却する。

注意:
- SSE（StreamingResponse）の場合、BackgroundTasks をレスポンスに紐づけないと実行されない。
  ここでは memory.py 側で積んだ非同期ジョブ（jobs テーブルへの enqueue）を確実に動かすため、
  StreamingResponse(background=...) を必ず設定する。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/chat")
def chat(
    request: schemas.ChatRequest,
    background_tasks: BackgroundTasks,
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """チャットリクエストを SSE ストリーミングで返却。"""
    generator = memory_manager.stream_chat(request, background_tasks)
    # --- BackgroundTasks を紐づける（これが無いと enqueue が実行されない） ---
    return StreamingResponse(generator, media_type="text/event-stream", background=background_tasks)
