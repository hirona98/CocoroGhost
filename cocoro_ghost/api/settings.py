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

    for reminder in db.query(models.Reminder).order_by(models.Reminder.scheduled_at.asc(), models.Reminder.id.asc()).all():
        reminders.append(
            schemas.ReminderSettings(
                scheduled_at=reminder.scheduled_at,
                content=reminder.content,
            )
        )

    for preset in db.query(models.LlmPreset).order_by(models.LlmPreset.id.asc()).all():
        llm_presets.append(
            schemas.LlmPresetSettings(
                llm_preset_id=preset.id,
                llm_preset_name=preset.name,
                system_prompt=preset.system_prompt,
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

    for preset in db.query(models.EmbeddingPreset).order_by(models.EmbeddingPreset.id.asc()).all():
        embedding_presets.append(
            schemas.EmbeddingPresetSettings(
                embedding_preset_id=preset.id,
                embedding_preset_name=preset.name,
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
        active_llm_preset_id=global_settings.active_llm_preset_id,
        active_embedding_preset_id=getattr(global_settings, "active_embedding_preset_id", None),
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

    # LLMプリセットの更新（複数件）
    llm_presets_by_id: dict[int, models.LlmPreset] = {
        int(p.id): p for p in db.query(models.LlmPreset).order_by(models.LlmPreset.id.asc()).all()
    }
    for lp in request.llm_preset:
        preset = llm_presets_by_id.get(int(lp.llm_preset_id))
        if preset is None:
            raise HTTPException(status_code=400, detail=f"llm_preset_id not found: {lp.llm_preset_id}")
        preset.name = lp.llm_preset_name
        preset.system_prompt = lp.system_prompt
        preset.llm_api_key = lp.llm_api_key
        preset.llm_model = lp.llm_model
        preset.reasoning_effort = lp.reasoning_effort
        preset.llm_base_url = lp.llm_base_url
        preset.max_turns_window = lp.max_turns_window
        preset.max_tokens = lp.max_tokens
        preset.image_model_api_key = lp.image_model_api_key
        preset.image_model = lp.image_model
        preset.image_llm_base_url = lp.image_llm_base_url
        preset.max_tokens_vision = lp.max_tokens_vision
        preset.image_timeout_seconds = lp.image_timeout_seconds

    # Embeddingプリセットの更新（複数件）
    embedding_presets_by_id: dict[int, models.EmbeddingPreset] = {
        int(p.id): p for p in db.query(models.EmbeddingPreset).order_by(models.EmbeddingPreset.id.asc()).all()
    }
    for ep in request.embedding_preset:
        preset = embedding_presets_by_id.get(int(ep.embedding_preset_id))
        if preset is None:
            raise HTTPException(status_code=400, detail=f"embedding_preset_id not found: {ep.embedding_preset_id}")
        preset.name = ep.embedding_preset_name
        preset.embedding_api_key = ep.embedding_model_api_key
        preset.embedding_model = ep.embedding_model
        preset.embedding_base_url = ep.embedding_base_url
        preset.embedding_dimension = ep.embedding_dimension
        preset.similar_episodes_limit = ep.similar_episodes_limit

    # アクティブIDの更新
    global_settings.active_llm_preset_id = request.active_llm_preset_id
    global_settings.active_embedding_preset_id = request.active_embedding_preset_id

    active_llm = db.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id).first()
    if not active_llm:
        raise HTTPException(status_code=400, detail="Active LLM preset is not set")
    active_embedding = db.query(models.EmbeddingPreset).filter_by(id=global_settings.active_embedding_preset_id).first()
    if not active_embedding:
        raise HTTPException(status_code=400, detail="Active embedding preset is not set")

    # 変更後の設定で RuntimeConfig を組み立て、利用可能なメモリDBかを先に検証する
    runtime_config = build_runtime_config(toml_config, global_settings, active_llm, active_embedding)
    try:
        init_memory_db(runtime_config.memory_id, runtime_config.embedding_dimension)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=f"memory DB init failed: {exc}") from exc

    db.commit()

    # 設定変更をランタイムへ即時反映
    db.expunge(global_settings)
    db.expunge(active_llm)
    db.expunge(active_embedding)
    set_global_config_store(ConfigStore(toml_config, runtime_config, global_settings, active_llm, active_embedding))
    reset_memory_manager()

    # 最新状態を返す
    return get_settings(db=db)
