"""
/v2/meta-request エンドポイント

システムからの指示（instruction）とペイロードを受け取り、
PERSONA_ANCHORの人物として能動的なメッセージを生成させる。
ユーザーに対して自然に話しかける機能として使用される。

注意:
- BackgroundTasks は Response に紐づけないと実行されない。
  本エンドポイントは 204 を返すため、Response(background=...) を明示する。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()


@router.post("/v2/meta-request", status_code=status.HTTP_204_NO_CONTENT)
def meta_request_v2(
    request: schemas.MetaRequestV2Request,
    background_tasks: BackgroundTasks,
    memory_manager: MemoryManager = Depends(get_memory_manager),
) -> Response:
    """メタ要求をUnit(Episode)として保存し、派生ジョブを積む。"""
    images = [{"type": "data_uri", "base64": schemas.data_uri_image_to_base64(s)} for s in request.images]
    internal = schemas.MetaRequestRequest(
        instruction=request.instruction,
        payload_text=request.payload_text,
        images=images,
    )
    memory_manager.handle_meta_request(internal, background_tasks=background_tasks)
    # --- BackgroundTasks を紐づける（これが無いと enqueue が実行されない） ---
    return Response(status_code=status.HTTP_204_NO_CONTENT, background=background_tasks)
