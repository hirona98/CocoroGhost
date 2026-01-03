"""
視覚（Vision）API

クライアント（CocoroConsole等）が画像取得を行い、
その結果（data URI画像）を CocoroGhost へ返すためのエンドポイントを提供する。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.memory import MemoryManager


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/v2/vision/capture-response", status_code=status.HTTP_204_NO_CONTENT)
def vision_capture_response_v2(
    request: schemas.VisionCaptureResponseV2Request,
    memory_manager: MemoryManager = Depends(get_memory_manager),
) -> Response:
    """
    クライアントからの画像取得結果を受け取り、待機中の要求へ紐づける。

    チャット視覚やデスクトップウォッチ側が request_id の応答待ちを解除できるようにする。
    """
    logger.info(
        "vision capture-response received request_id=%s client_id=%s images_count=%s has_error=%s",
        request.request_id,
        request.client_id,
        len(request.images or []),
        bool(request.error),
    )
    memory_manager.handle_vision_capture_response(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
