"""設定API（分離後）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.db import load_global_settings
from cocoro_ghost.deps import get_settings_db_dep

router = APIRouter()


@router.get("/settings", response_model=schemas.FullSettingsResponse)
async def get_settings(
    db: Session = Depends(get_settings_db_dep),
):
    """全設定を取得（GlobalSettings + アクティブなプリセット情報）。"""
    global_settings = load_global_settings(db)

    llm_presets: list[schemas.LlmPresetSettings] = []
    embedding_presets: list[schemas.EmbeddingPresetSettings] = []

    character_preset = None
    if global_settings.active_character_preset_id:
        preset = db.query(models.CharacterPreset).filter_by(id=global_settings.active_character_preset_id).first()
        if preset:
            character_preset = preset

    if global_settings.active_llm_preset_id:
        preset = db.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id).first()
        if preset:
            llm_presets.append(
                schemas.LlmPresetSettings(
                    llm_preset_id=preset.id,
                    llm_preset_name=preset.name,
                    system_prompt=character_preset.system_prompt if character_preset else "",
                    llm_api_key=preset.llm_api_key,
                    llm_model=preset.llm_model,
                    reasoning_effort=preset.reasoning_effort,
                    llm_base_url=preset.llm_base_url,
                    max_turns_window=preset.max_turns_window,
                    max_tokens=preset.max_tokens,
                    image_model_api_key=preset.image_model_api_key,
                    image_model=preset.image_model,
                    image_llm_base_url=preset.image_llm_base_url,
                    max_tokens_vision=preset.max_tokens_vision,
                    image_timeout_seconds=preset.image_timeout_seconds,
                )
            )

            embedding_presets.append(
                schemas.EmbeddingPresetSettings(
                    embedding_preset_id=character_preset.id if character_preset else preset.id,
                    embedding_preset_name=character_preset.memory_id if character_preset else preset.name,
                    embedding_model_api_key=preset.embedding_api_key,
                    embedding_model=preset.embedding_model,
                    embedding_base_url=preset.embedding_base_url,
                    embedding_dimension=preset.embedding_dimension,
                    similar_episodes_limit=preset.similar_episodes_limit,
                )
            )

    return schemas.FullSettingsResponse(
        exclude_keywords=json.loads(global_settings.exclude_keywords),
        llm_preset=llm_presets,
        embedding_preset=embedding_presets,
    )


@router.post("/settings", response_model=schemas.FullSettingsResponse)
async def update_settings(
    request: schemas.FullSettingsUpdateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """全設定をまとめて更新（アクティブなプリセットと共通設定）。"""
    global_settings = load_global_settings(db)

    # 共通設定更新
    if request.exclude_keywords is not None:
        global_settings.exclude_keywords = json.dumps(request.exclude_keywords)

    # アクティブなプリセットをロード
    active_llm = None
    if global_settings.active_llm_preset_id:
        active_llm = db.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id).first()
    active_character = None
    if global_settings.active_character_preset_id:
        active_character = db.query(models.CharacterPreset).filter_by(id=global_settings.active_character_preset_id).first()

    if request.llm_preset:
        if not active_llm:
            raise HTTPException(status_code=400, detail="Active LLM preset is not set")
        # 先頭のみ反映（単一アクティブ想定）
        lp = request.llm_preset[0]
        active_llm.llm_api_key = lp.llm_api_key or active_llm.llm_api_key
        active_llm.llm_model = lp.llm_model
        active_llm.reasoning_effort = lp.reasoning_effort
        active_llm.llm_base_url = lp.llm_base_url
        active_llm.max_turns_window = lp.max_turns_window
        active_llm.max_tokens = lp.max_tokens
        active_llm.image_model_api_key = lp.image_model_api_key
        active_llm.image_model = lp.image_model
        active_llm.image_llm_base_url = lp.image_llm_base_url
        active_llm.max_tokens_vision = lp.max_tokens_vision
        active_llm.image_timeout_seconds = lp.image_timeout_seconds
        if active_character and lp.system_prompt:
            active_character.system_prompt = lp.system_prompt

    if request.embedding_preset:
        if not active_llm:
            raise HTTPException(status_code=400, detail="Active LLM preset is not set")
        ep = request.embedding_preset[0]
        active_llm.embedding_api_key = ep.embedding_model_api_key or active_llm.embedding_api_key
        active_llm.embedding_model = ep.embedding_model
        active_llm.embedding_base_url = ep.embedding_base_url
        active_llm.embedding_dimension = ep.embedding_dimension
        active_llm.similar_episodes_limit = ep.similar_episodes_limit
        if active_character:
            active_character.memory_id = ep.embedding_preset_name

    db.commit()

    # 最新状態を返す
    return await get_settings(db=db)
