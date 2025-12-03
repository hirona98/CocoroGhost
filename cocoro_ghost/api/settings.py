"""設定API（分離後）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.db import load_global_settings
from cocoro_ghost.deps import get_settings_db_dep

router = APIRouter()


# --- 共通設定 ---


@router.get("/settings", response_model=schemas.FullSettingsResponse)
async def get_settings(
    db: Session = Depends(get_settings_db_dep),
):
    """全設定を取得（GlobalSettings + アクティブなプリセット情報）。"""
    global_settings = load_global_settings(db)

    llm_preset = None
    if global_settings.active_llm_preset_id:
        preset = db.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id).first()
        if preset:
            llm_preset = schemas.LlmPresetResponse.model_validate(preset)

    character_preset = None
    if global_settings.active_character_preset_id:
        preset = db.query(models.CharacterPreset).filter_by(id=global_settings.active_character_preset_id).first()
        if preset:
            character_preset = schemas.CharacterPresetResponse.model_validate(preset)

    return schemas.FullSettingsResponse(
        exclude_keywords=json.loads(global_settings.exclude_keywords),
        llm_preset=llm_preset,
        character_preset=character_preset,
    )


@router.patch("/settings", response_model=schemas.GlobalSettingsResponse)
async def update_settings(
    request: schemas.GlobalSettingsUpdateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """共通設定（GlobalSettings）を更新。"""
    global_settings = load_global_settings(db)

    if request.exclude_keywords is not None:
        global_settings.exclude_keywords = json.dumps(request.exclude_keywords)

    db.commit()

    return schemas.GlobalSettingsResponse(
        exclude_keywords=json.loads(global_settings.exclude_keywords),
        active_llm_preset_id=global_settings.active_llm_preset_id,
        active_character_preset_id=global_settings.active_character_preset_id,
    )


# --- LLMプリセット ---


@router.get("/llm-presets", response_model=schemas.LlmPresetsListResponse)
async def list_llm_presets(db: Session = Depends(get_settings_db_dep)):
    """LLMプリセット一覧を取得。"""
    presets = db.query(models.LlmPreset).order_by(models.LlmPreset.name).all()
    global_settings = load_global_settings(db)

    return schemas.LlmPresetsListResponse(
        presets=[schemas.LlmPresetSummary.model_validate(p) for p in presets],
        active_id=global_settings.active_llm_preset_id,
    )


@router.post("/llm-presets", status_code=201, response_model=schemas.LlmPresetResponse)
async def create_llm_preset(
    request: schemas.LlmPresetCreateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """新しいLLMプリセットを作成。"""
    existing = db.query(models.LlmPreset).filter_by(name=request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"LLM preset '{request.name}' already exists")

    preset = models.LlmPreset(
        name=request.name,
        llm_api_key=request.llm_api_key,
        llm_model=request.llm_model,
        llm_base_url=request.llm_base_url,
        reasoning_effort=request.reasoning_effort,
        max_turns_window=request.max_turns_window,
        max_tokens_vision=request.max_tokens_vision,
        max_tokens=request.max_tokens,
        embedding_model=request.embedding_model,
        embedding_api_key=request.embedding_api_key,
        embedding_base_url=request.embedding_base_url,
        embedding_dimension=request.embedding_dimension,
        image_model=request.image_model,
        image_model_api_key=request.image_model_api_key,
        image_llm_base_url=request.image_llm_base_url,
        image_timeout_seconds=request.image_timeout_seconds,
        similar_episodes_limit=request.similar_episodes_limit,
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)

    return schemas.LlmPresetResponse.model_validate(preset)


@router.get("/llm-presets/{preset_id}", response_model=schemas.LlmPresetResponse)
async def get_llm_preset(preset_id: int, db: Session = Depends(get_settings_db_dep)):
    """LLMプリセットの詳細を取得。"""
    preset = db.query(models.LlmPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"LLM preset (id={preset_id}) not found")

    return schemas.LlmPresetResponse.model_validate(preset)


@router.patch("/llm-presets/{preset_id}", response_model=schemas.LlmPresetResponse)
async def update_llm_preset(
    preset_id: int,
    request: schemas.LlmPresetUpdateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """LLMプリセットを更新。"""
    preset = db.query(models.LlmPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"LLM preset (id={preset_id}) not found")

    if request.llm_api_key is not None:
        preset.llm_api_key = request.llm_api_key
    if request.llm_model is not None:
        preset.llm_model = request.llm_model
    if request.llm_base_url is not None:
        preset.llm_base_url = request.llm_base_url
    if request.reasoning_effort is not None:
        preset.reasoning_effort = request.reasoning_effort
    if request.max_turns_window is not None:
        preset.max_turns_window = request.max_turns_window
    if request.max_tokens_vision is not None:
        preset.max_tokens_vision = request.max_tokens_vision
    if request.max_tokens is not None:
        preset.max_tokens = request.max_tokens
    if request.embedding_model is not None:
        preset.embedding_model = request.embedding_model
    if request.embedding_api_key is not None:
        preset.embedding_api_key = request.embedding_api_key
    if request.embedding_base_url is not None:
        preset.embedding_base_url = request.embedding_base_url
    if request.embedding_dimension is not None:
        preset.embedding_dimension = request.embedding_dimension
    if request.image_model is not None:
        preset.image_model = request.image_model
    if request.image_model_api_key is not None:
        preset.image_model_api_key = request.image_model_api_key
    if request.image_llm_base_url is not None:
        preset.image_llm_base_url = request.image_llm_base_url
    if request.image_timeout_seconds is not None:
        preset.image_timeout_seconds = request.image_timeout_seconds
    if request.similar_episodes_limit is not None:
        preset.similar_episodes_limit = request.similar_episodes_limit

    db.commit()
    db.refresh(preset)

    return schemas.LlmPresetResponse.model_validate(preset)


@router.delete("/llm-presets/{preset_id}", status_code=204)
async def delete_llm_preset(preset_id: int, db: Session = Depends(get_settings_db_dep)):
    """LLMプリセットを削除。"""
    preset = db.query(models.LlmPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"LLM preset (id={preset_id}) not found")

    global_settings = load_global_settings(db)
    if global_settings.active_llm_preset_id == preset_id:
        raise HTTPException(status_code=400, detail="Cannot delete active LLM preset")

    db.delete(preset)
    db.commit()


@router.post("/llm-presets/{preset_id}/activate", response_model=schemas.ActivateResponse)
async def activate_llm_preset(preset_id: int, db: Session = Depends(get_settings_db_dep)):
    """LLMプリセットをアクティブ化（再起動が必要）。"""
    preset = db.query(models.LlmPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"LLM preset (id={preset_id}) not found")

    global_settings = load_global_settings(db)
    global_settings.active_llm_preset_id = preset_id
    db.commit()

    return schemas.ActivateResponse(
        message=f"Activated LLM preset '{preset.name}'. Please restart the application.",
        restart_required=True,
    )


# --- キャラクタープリセット ---


@router.get("/character-presets", response_model=schemas.CharacterPresetsListResponse)
async def list_character_presets(db: Session = Depends(get_settings_db_dep)):
    """キャラクタープリセット一覧を取得。"""
    presets = db.query(models.CharacterPreset).order_by(models.CharacterPreset.name).all()
    global_settings = load_global_settings(db)

    return schemas.CharacterPresetsListResponse(
        presets=[schemas.CharacterPresetSummary.model_validate(p) for p in presets],
        active_id=global_settings.active_character_preset_id,
    )


@router.post("/character-presets", status_code=201, response_model=schemas.CharacterPresetResponse)
async def create_character_preset(
    request: schemas.CharacterPresetCreateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """新しいキャラクタープリセットを作成。"""
    existing = db.query(models.CharacterPreset).filter_by(name=request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Character preset '{request.name}' already exists")

    preset = models.CharacterPreset(
        name=request.name,
        system_prompt=request.system_prompt,
        memory_id=request.memory_id,
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)

    return schemas.CharacterPresetResponse.model_validate(preset)


@router.get("/character-presets/{preset_id}", response_model=schemas.CharacterPresetResponse)
async def get_character_preset(preset_id: int, db: Session = Depends(get_settings_db_dep)):
    """キャラクタープリセットの詳細を取得。"""
    preset = db.query(models.CharacterPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"Character preset (id={preset_id}) not found")

    return schemas.CharacterPresetResponse.model_validate(preset)


@router.patch("/character-presets/{preset_id}", response_model=schemas.CharacterPresetResponse)
async def update_character_preset(
    preset_id: int,
    request: schemas.CharacterPresetUpdateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """キャラクタープリセットを更新。"""
    preset = db.query(models.CharacterPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"Character preset (id={preset_id}) not found")

    if request.system_prompt is not None:
        preset.system_prompt = request.system_prompt
    if request.memory_id is not None:
        preset.memory_id = request.memory_id

    db.commit()
    db.refresh(preset)

    return schemas.CharacterPresetResponse.model_validate(preset)


@router.delete("/character-presets/{preset_id}", status_code=204)
async def delete_character_preset(preset_id: int, db: Session = Depends(get_settings_db_dep)):
    """キャラクタープリセットを削除。"""
    preset = db.query(models.CharacterPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"Character preset (id={preset_id}) not found")

    global_settings = load_global_settings(db)
    if global_settings.active_character_preset_id == preset_id:
        raise HTTPException(status_code=400, detail="Cannot delete active character preset")

    db.delete(preset)
    db.commit()


@router.post("/character-presets/{preset_id}/activate", response_model=schemas.ActivateResponse)
async def activate_character_preset(preset_id: int, db: Session = Depends(get_settings_db_dep)):
    """キャラクタープリセットをアクティブ化（再起動が必要）。"""
    preset = db.query(models.CharacterPreset).filter_by(id=preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"Character preset (id={preset_id}) not found")

    global_settings = load_global_settings(db)
    global_settings.active_character_preset_id = preset_id
    db.commit()

    return schemas.ActivateResponse(
        message=f"Activated character preset '{preset.name}'. Please restart the application.",
        restart_required=True,
    )
