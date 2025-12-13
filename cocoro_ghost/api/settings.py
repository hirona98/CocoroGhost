"""設定API（分離後）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.db import load_global_settings
from cocoro_ghost.deps import reset_memory_manager
from cocoro_ghost.deps import get_settings_db_dep

router = APIRouter()


@router.get("/settings", response_model=schemas.FullSettingsResponse)
def get_settings(
    db: Session = Depends(get_settings_db_dep),
):
    """全設定を取得（GlobalSettings + アクティブなプリセット情報）。"""
    global_settings = load_global_settings(db)

    llm_presets: list[schemas.LlmPresetSettings] = []
    embedding_presets: list[schemas.EmbeddingPresetSettings] = []
    reminders: list[schemas.ReminderSettings] = []

    character_preset = None
    if global_settings.active_character_preset_id:
        preset = db.query(models.CharacterPreset).filter_by(id=global_settings.active_character_preset_id).first()
        if preset:
            character_preset = preset

    for reminder in db.query(models.Reminder).order_by(models.Reminder.scheduled_at.asc(), models.Reminder.id.asc()).all():
        reminders.append(
            schemas.ReminderSettings(
                scheduled_at=reminder.scheduled_at,
                content=reminder.content,
            )
        )

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
        reminders_enabled=global_settings.reminders_enabled,
        reminders=reminders,
        llm_preset=llm_presets,
        embedding_preset=embedding_presets,
    )


@router.post("/settings", response_model=schemas.FullSettingsResponse)
def update_settings(
    request: schemas.FullSettingsUpdateRequest,
    db: Session = Depends(get_settings_db_dep),
):
    """全設定をまとめて更新（アクティブなプリセットと共通設定）。"""
    from cocoro_ghost.config import ConfigStore, build_runtime_config, get_config_store, set_global_config_store
    from cocoro_ghost.db import init_memory_db

    current_store = get_config_store()
    toml_config = current_store.toml_config
    global_settings = load_global_settings(db)

    # 共通設定更新
    global_settings.exclude_keywords = json.dumps(request.exclude_keywords)
    global_settings.reminders_enabled = request.reminders_enabled

    # リマインダー更新：常に「全置き換え」（IDは作り直される）
    db.query(models.Reminder).delete(synchronize_session=False)
    for item in request.reminders:
        db.add(
            models.Reminder(
                scheduled_at=item.scheduled_at,
                content=item.content,
            )
        )

    # アクティブなプリセットをロード
    active_llm = None
    if global_settings.active_llm_preset_id:
        active_llm = db.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id).first()
    active_character = None
    if global_settings.active_character_preset_id:
        active_character = db.query(models.CharacterPreset).filter_by(id=global_settings.active_character_preset_id).first()

    if not active_llm:
        raise HTTPException(status_code=400, detail="Active LLM preset is not set")
    if not active_character:
        raise HTTPException(status_code=400, detail="Active character preset is not set")

    # 先頭のみ反映（単一アクティブ想定）
    if len(request.llm_preset) != 1:
        raise HTTPException(status_code=400, detail="llm_preset must have exactly 1 item")
    lp = request.llm_preset[0]
    active_llm.llm_api_key = lp.llm_api_key
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
    active_character.system_prompt = lp.system_prompt

    if len(request.embedding_preset) != 1:
        raise HTTPException(status_code=400, detail="embedding_preset must have exactly 1 item")
    ep = request.embedding_preset[0]
    active_llm.embedding_api_key = ep.embedding_model_api_key
    active_llm.embedding_model = ep.embedding_model
    active_llm.embedding_base_url = ep.embedding_base_url
    active_llm.embedding_dimension = ep.embedding_dimension
    active_llm.similar_episodes_limit = ep.similar_episodes_limit
    active_character.memory_id = ep.embedding_preset_name

    # 変更後の設定で RuntimeConfig を組み立て、利用可能なメモリDBかを先に検証する
    runtime_config = build_runtime_config(toml_config, global_settings, active_llm, active_character)
    try:
        init_memory_db(runtime_config.memory_id, runtime_config.embedding_dimension)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=f"memory DB init failed: {exc}") from exc

    db.commit()

    # 設定変更をランタイムへ即時反映
    db.expunge(global_settings)
    db.expunge(active_llm)
    db.expunge(active_character)
    set_global_config_store(ConfigStore(toml_config, runtime_config, global_settings, active_llm, active_character))
    reset_memory_manager()

    # 最新状態を返す
    return get_settings(db=db)
