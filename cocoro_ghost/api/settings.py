"""/settings エンドポイント。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.db import get_db
from cocoro_ghost.deps import get_config_store_dep


router = APIRouter()


@router.get("/settings", response_model=schemas.SettingsFullResponse)
async def get_settings(config_store = Depends(get_config_store_dep)):
    """現在の設定を取得"""
    cfg = config_store.config
    return schemas.SettingsFullResponse(
        preset_name=cfg.preset_name,
        llm_api_key=cfg.llm_api_key,
        llm_model=cfg.llm_model,
        reflection_model=cfg.reflection_model,
        embedding_model=cfg.embedding_model,
        embedding_dimension=cfg.embedding_dimension,
        image_model=cfg.image_model,
        image_timeout_seconds=cfg.image_timeout_seconds,
        character_prompt=cfg.character_prompt,
        intervention_level=cfg.intervention_level,
        exclude_keywords=cfg.exclude_keywords,
        similar_episodes_limit=cfg.similar_episodes_limit,
        max_chat_queue=cfg.max_chat_queue,
    )


@router.post("/settings", response_model=schemas.SettingsResponse)
async def update_settings(
    request: schemas.SettingsUpdateRequest,
    db: Session = Depends(get_db),
):
    """アクティブなプリセットの設定を更新（DBに永続化、再起動時に反映）"""
    preset = db.query(models.SettingPreset).filter_by(is_active=True).one()

    if request.exclude_keywords is not None:
        preset.exclude_keywords = json.dumps(request.exclude_keywords)
    if request.character_prompt is not None:
        preset.character_prompt = request.character_prompt
    if request.intervention_level is not None:
        preset.intervention_level = request.intervention_level

    db.commit()

    return schemas.SettingsResponse(
        exclude_keywords=json.loads(preset.exclude_keywords),
        character_prompt=preset.character_prompt,
        intervention_level=preset.intervention_level,
    )


@router.get("/presets", response_model=schemas.PresetsListResponse)
async def list_presets(db: Session = Depends(get_db)):
    """プリセット一覧を取得"""
    presets = db.query(models.SettingPreset).order_by(models.SettingPreset.name).all()
    return schemas.PresetsListResponse(
        presets=[
            schemas.PresetSummary(
                name=p.name,
                is_active=p.is_active,
                created_at=p.created_at,
            )
            for p in presets
        ]
    )


@router.post("/presets", status_code=201)
async def create_preset(request: schemas.PresetCreateRequest, db: Session = Depends(get_db)):
    """新しいプリセットを作成"""
    existing = db.query(models.SettingPreset).filter_by(name=request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Preset '{request.name}' already exists")

    preset = models.SettingPreset(
        name=request.name,
        is_active=False,
        llm_api_key=request.llm_api_key,
        llm_model=request.llm_model,
        reflection_model=request.reflection_model,
        embedding_model=request.embedding_model,
        embedding_dimension=request.embedding_dimension,
        image_model=request.image_model,
        image_timeout_seconds=request.image_timeout_seconds,
        character_prompt=request.character_prompt,
        intervention_level=request.intervention_level,
        exclude_keywords=json.dumps(request.exclude_keywords),
        similar_episodes_limit=request.similar_episodes_limit,
        max_chat_queue=request.max_chat_queue,
    )
    db.add(preset)
    db.commit()
    return {"message": f"Preset '{request.name}' created"}


@router.patch("/presets/{name}")
async def update_preset(name: str, request: schemas.PresetUpdateRequest, db: Session = Depends(get_db)):
    """プリセットの内容を更新"""
    preset = db.query(models.SettingPreset).filter_by(name=name).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")

    if request.llm_api_key is not None:
        preset.llm_api_key = request.llm_api_key
    if request.llm_model is not None:
        preset.llm_model = request.llm_model
    if request.reflection_model is not None:
        preset.reflection_model = request.reflection_model
    if request.embedding_model is not None:
        preset.embedding_model = request.embedding_model
    if request.embedding_dimension is not None:
        preset.embedding_dimension = request.embedding_dimension
    if request.image_model is not None:
        preset.image_model = request.image_model
    if request.image_timeout_seconds is not None:
        preset.image_timeout_seconds = request.image_timeout_seconds
    if request.character_prompt is not None:
        preset.character_prompt = request.character_prompt
    if request.intervention_level is not None:
        preset.intervention_level = request.intervention_level
    if request.exclude_keywords is not None:
        preset.exclude_keywords = json.dumps(request.exclude_keywords)
    if request.similar_episodes_limit is not None:
        preset.similar_episodes_limit = request.similar_episodes_limit
    if request.max_chat_queue is not None:
        preset.max_chat_queue = request.max_chat_queue

    db.commit()
    return {"message": f"Preset '{name}' updated", "restart_required": preset.is_active}


@router.delete("/presets/{name}", status_code=204)
async def delete_preset(name: str, db: Session = Depends(get_db)):
    """プリセットを削除"""
    preset = db.query(models.SettingPreset).filter_by(name=name).first()
    if not preset:
        raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")

    if preset.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete active preset")

    db.delete(preset)
    db.commit()


@router.post("/presets/{name}/activate", response_model=schemas.PresetActivateResponse)
async def activate_preset(name: str, db: Session = Depends(get_db)):
    """指定したプリセットをアクティブにする（再起動が必要）"""
    target = db.query(models.SettingPreset).filter_by(name=name).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")

    db.query(models.SettingPreset).update({"is_active": False})
    target.is_active = True
    db.commit()

    return schemas.PresetActivateResponse(
        message=f"Activated preset '{name}'. Please restart the application.",
        active_preset=name,
        restart_required=True,
    )
