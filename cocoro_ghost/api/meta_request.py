"""/v1/meta_request エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/v1/meta_request", status_code=status.HTTP_204_NO_CONTENT)
def meta_request_v1(
    request: schemas.MetaRequestV1Request,
    background_tasks: BackgroundTasks,
    memory_manager: MemoryManager = Depends(get_memory_manager),
) -> Response:
    """メタ要求をUnit(Episode)として保存し、派生ジョブを積む。"""
    images = [{"type": "data_uri", "base64": schemas.data_uri_image_to_base64(s)} for s in request.images]
    internal = schemas.MetaRequestRequest(instruction=request.prompt, payload_text="", images=images)
    memory_manager.handle_meta_request(internal, background_tasks=background_tasks)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
