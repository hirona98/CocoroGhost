"""/settings エンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_config_store_dep


router = APIRouter()


@router.post("/settings", response_model=schemas.SettingsResponse)
async def update_settings(
    request: schemas.SettingsUpdateRequest,
    config_store = Depends(get_config_store_dep),
):
    updated = config_store.update(
        exclude_keywords=request.exclude_keywords,
        character_prompt=request.character_prompt,
        intervention_level=request.intervention_level,
    )
    return schemas.SettingsResponse(
        exclude_keywords=updated.exclude_keywords,
        character_prompt=updated.character_prompt,
        intervention_level=updated.intervention_level,
    )
