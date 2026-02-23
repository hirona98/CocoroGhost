"""
設定API

アプリケーション設定の取得・更新を行うエンドポイント。
LLM/Embedding/PersonaPreset/AddonPreset各プリセットの管理、
GlobalSettings（除外キーワード、記憶機能=常時ON等）の更新、
デスクトップウォッチ設定の管理をサポートする。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.db import load_global_settings
from cocoro_ghost.deps import reset_memory_manager
from cocoro_ghost.deps import get_settings_db_dep

router = APIRouter()


def _ensure_unique_ids(kind: str, ids: list[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            duplicates.add(item_id)
        seen.add(item_id)
    if duplicates:
        d = ", ".join(sorted(duplicates))
        raise HTTPException(status_code=400, detail=f"duplicate {kind} ids: {d}")


@router.get("/settings", response_model=schemas.FullSettingsResponse)
def get_settings(
    db: Session = Depends(get_settings_db_dep),
):
    """全設定を取得（GlobalSettings + アクティブなプリセット情報）。"""
    global_settings = load_global_settings(db)
    # 記憶機能は常時有効のため、過去データに false が残っていても true に正規化する。
    if not bool(global_settings.memory_enabled):
        global_settings.memory_enabled = True
        db.commit()
        db.refresh(global_settings)

    llm_presets: list[schemas.LlmPresetSettings] = []
    embedding_presets: list[schemas.EmbeddingPresetSettings] = []
    persona_presets: list[schemas.PersonaPresetSettings] = []
    addon_presets: list[schemas.AddonPresetSettings] = []

    for preset in (
        db.query(models.LlmPreset).filter_by(archived=False).order_by(models.LlmPreset.id.asc()).all()
    ):
        llm_presets.append(
            schemas.LlmPresetSettings(
                llm_preset_id=preset.id,
                llm_preset_name=preset.name,
                llm_api_key=preset.llm_api_key,
                llm_model=preset.llm_model,
                reasoning_effort=preset.reasoning_effort,
                reply_web_search_enabled=bool(preset.reply_web_search_enabled),
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

    for preset in (
        db.query(models.EmbeddingPreset).filter_by(archived=False).order_by(models.EmbeddingPreset.id.asc()).all()
    ):
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

    for preset in (
        db.query(models.PersonaPreset).filter_by(archived=False).order_by(models.PersonaPreset.id.asc()).all()
    ):
        persona_presets.append(
            schemas.PersonaPresetSettings(
                persona_preset_id=preset.id,
                persona_preset_name=preset.name,
                persona_text=preset.persona_text,
                second_person_label=preset.second_person_label,
            )
        )

    for preset in (
        db.query(models.AddonPreset).filter_by(archived=False).order_by(models.AddonPreset.id.asc()).all()
    ):
        addon_presets.append(
            schemas.AddonPresetSettings(
                addon_preset_id=preset.id,
                addon_preset_name=preset.name,
                addon_text=preset.addon_text,
            )
        )

    return schemas.FullSettingsResponse(
        memory_enabled=global_settings.memory_enabled,
        desktop_watch_enabled=bool(global_settings.desktop_watch_enabled),
        desktop_watch_interval_seconds=int(global_settings.desktop_watch_interval_seconds),
        desktop_watch_target_client_id=(
            str(global_settings.desktop_watch_target_client_id).strip()
            if global_settings.desktop_watch_target_client_id is not None
            else None
        ),
        autonomy_enabled=bool(getattr(global_settings, "autonomy_enabled", False)),
        autonomy_heartbeat_seconds=max(1, int(getattr(global_settings, "autonomy_heartbeat_seconds", 30))),
        autonomy_max_parallel_intents=max(1, int(getattr(global_settings, "autonomy_max_parallel_intents", 2))),
        camera_watch_enabled=bool(getattr(global_settings, "camera_watch_enabled", False)),
        camera_watch_interval_seconds=max(1, int(getattr(global_settings, "camera_watch_interval_seconds", 15))),
        active_llm_preset_id=global_settings.active_llm_preset_id,
        active_embedding_preset_id=global_settings.active_embedding_preset_id,
        active_persona_preset_id=global_settings.active_persona_preset_id,
        active_addon_preset_id=global_settings.active_addon_preset_id,
        llm_preset=llm_presets,
        embedding_preset=embedding_presets,
        persona_preset=persona_presets,
        addon_preset=addon_presets,
    )


@router.put("/settings", response_model=schemas.FullSettingsResponse)
def commit_settings(
    request: schemas.FullSettingsUpdateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_settings_db_dep),
):
    """全設定をまとめて確定（全置換 + アーカイブ + active IDs）。"""
    from cocoro_ghost.config import ConfigStore, build_runtime_config, get_config_store, set_global_config_store
    from cocoro_ghost.db import init_memory_db

    current_store = get_config_store()
    toml_config = current_store.toml_config
    global_settings = load_global_settings(db)

    llm_ids = [str(x.llm_preset_id) for x in request.llm_preset]
    embedding_ids = [str(x.embedding_preset_id) for x in request.embedding_preset]
    persona_ids = [str(x.persona_preset_id) for x in request.persona_preset]
    addon_ids = [str(x.addon_preset_id) for x in request.addon_preset]
    _ensure_unique_ids("llm_preset", llm_ids)
    _ensure_unique_ids("embedding_preset", embedding_ids)
    _ensure_unique_ids("persona_preset", persona_ids)
    _ensure_unique_ids("addon_preset", addon_ids)

    if str(request.active_llm_preset_id) not in set(llm_ids):
        raise HTTPException(status_code=400, detail="active_llm_preset_id must be included in llm_preset list")
    if str(request.active_embedding_preset_id) not in set(embedding_ids):
        raise HTTPException(status_code=400, detail="active_embedding_preset_id must be included in embedding_preset list")
    if str(request.active_persona_preset_id) not in set(persona_ids):
        raise HTTPException(status_code=400, detail="active_persona_preset_id must be included in persona_preset list")
    if str(request.active_addon_preset_id) not in set(addon_ids):
        raise HTTPException(status_code=400, detail="active_addon_preset_id must be included in addon_preset list")

    # 共通設定更新
    global_settings.memory_enabled = request.memory_enabled
    global_settings.desktop_watch_enabled = bool(request.desktop_watch_enabled)
    global_settings.desktop_watch_interval_seconds = int(request.desktop_watch_interval_seconds)
    # --- desktop_watch_target_client_id は「明示指定されたときだけ更新」する ---
    # NOTE:
    # - Web UI はこの項目を編集しないため、未送信時は既存DB値を保持する。
    # - 対応クライアントから明示的に null/空文字が来た場合のみ None へ更新する。
    if "desktop_watch_target_client_id" in request.model_fields_set:
        global_settings.desktop_watch_target_client_id = (
            str(request.desktop_watch_target_client_id).strip() if request.desktop_watch_target_client_id else None
        )
    global_settings.autonomy_enabled = bool(request.autonomy_enabled)
    global_settings.autonomy_heartbeat_seconds = max(1, int(request.autonomy_heartbeat_seconds))
    global_settings.autonomy_max_parallel_intents = max(1, int(request.autonomy_max_parallel_intents))
    global_settings.camera_watch_enabled = bool(request.camera_watch_enabled)
    global_settings.camera_watch_interval_seconds = max(1, int(request.camera_watch_interval_seconds))

    # LLMプリセットの更新（複数件 / 全置換 + アーカイブ）
    llm_existing = db.query(models.LlmPreset).order_by(models.LlmPreset.id.asc()).all()
    llm_presets_by_id: dict[str, models.LlmPreset] = {str(p.id): p for p in llm_existing}
    for lp in request.llm_preset:
        preset_id = str(lp.llm_preset_id)
        preset = llm_presets_by_id.get(preset_id)
        if preset is None:
            preset = models.LlmPreset(
                id=preset_id,
                name=lp.llm_preset_name,
                archived=False,
                llm_api_key=lp.llm_api_key,
                llm_model=lp.llm_model,
                reasoning_effort=lp.reasoning_effort,
                reply_web_search_enabled=bool(lp.reply_web_search_enabled),
                llm_base_url=lp.llm_base_url,
                max_turns_window=lp.max_turns_window,
                max_tokens=lp.max_tokens,
                image_model_api_key=lp.image_model_api_key,
                image_model=lp.image_model,
                image_llm_base_url=lp.image_llm_base_url,
                max_tokens_vision=lp.max_tokens_vision,
                image_timeout_seconds=lp.image_timeout_seconds,
            )
            db.add(preset)
            llm_presets_by_id[preset_id] = preset
        else:
            preset.archived = False
            preset.name = lp.llm_preset_name
            preset.llm_api_key = lp.llm_api_key
            preset.llm_model = lp.llm_model
            preset.reasoning_effort = lp.reasoning_effort
            preset.reply_web_search_enabled = bool(lp.reply_web_search_enabled)
            preset.llm_base_url = lp.llm_base_url
            preset.max_turns_window = lp.max_turns_window
            preset.max_tokens = lp.max_tokens
            preset.image_model_api_key = lp.image_model_api_key
            preset.image_model = lp.image_model
            preset.image_llm_base_url = lp.image_llm_base_url
            preset.max_tokens_vision = lp.max_tokens_vision
            preset.image_timeout_seconds = lp.image_timeout_seconds

    llm_id_set = set(llm_ids)
    for preset in llm_existing:
        if str(preset.id) not in llm_id_set:
            preset.archived = True

    # Embeddingプリセットの更新（複数件 / 全置換 + アーカイブ）
    embedding_existing = db.query(models.EmbeddingPreset).order_by(models.EmbeddingPreset.id.asc()).all()
    embedding_presets_by_id: dict[str, models.EmbeddingPreset] = {str(p.id): p for p in embedding_existing}
    for ep in request.embedding_preset:
        preset_id = str(ep.embedding_preset_id)
        preset = embedding_presets_by_id.get(preset_id)
        if preset is None:
            preset = models.EmbeddingPreset(
                id=preset_id,
                name=ep.embedding_preset_name,
                archived=False,
                embedding_api_key=ep.embedding_model_api_key,
                embedding_model=ep.embedding_model,
                embedding_base_url=ep.embedding_base_url,
                embedding_dimension=ep.embedding_dimension,
                similar_episodes_limit=ep.similar_episodes_limit,
            )
            db.add(preset)
            embedding_presets_by_id[preset_id] = preset
        else:
            preset.archived = False
            preset.name = ep.embedding_preset_name
            preset.embedding_api_key = ep.embedding_model_api_key
            preset.embedding_model = ep.embedding_model
            preset.embedding_base_url = ep.embedding_base_url
            preset.embedding_dimension = ep.embedding_dimension
            preset.similar_episodes_limit = ep.similar_episodes_limit

    embedding_id_set = set(embedding_ids)
    for preset in embedding_existing:
        if str(preset.id) not in embedding_id_set:
            preset.archived = True

    # Personaプリセットの更新（複数件 / 全置換 + アーカイブ）
    persona_existing = db.query(models.PersonaPreset).order_by(models.PersonaPreset.id.asc()).all()
    persona_presets_by_id: dict[str, models.PersonaPreset] = {str(p.id): p for p in persona_existing}
    for pp in request.persona_preset:
        preset_id = str(pp.persona_preset_id)
        preset = persona_presets_by_id.get(preset_id)
        if preset is None:
            preset = models.PersonaPreset(
                id=preset_id,
                name=pp.persona_preset_name,
                archived=False,
                persona_text=pp.persona_text,
                second_person_label=pp.second_person_label,
            )
            db.add(preset)
            persona_presets_by_id[preset_id] = preset
        else:
            preset.archived = False
            preset.name = pp.persona_preset_name
            preset.persona_text = pp.persona_text
            preset.second_person_label = pp.second_person_label

    persona_id_set = set(persona_ids)
    for preset in persona_existing:
        if str(preset.id) not in persona_id_set:
            preset.archived = True

    # Contractプリセットの更新（複数件 / 全置換 + アーカイブ）
    addon_existing = db.query(models.AddonPreset).order_by(models.AddonPreset.id.asc()).all()
    addon_presets_by_id: dict[str, models.AddonPreset] = {str(p.id): p for p in addon_existing}
    for ap in request.addon_preset:
        preset_id = str(ap.addon_preset_id)
        preset = addon_presets_by_id.get(preset_id)
        if preset is None:
            preset = models.AddonPreset(
                id=preset_id,
                name=ap.addon_preset_name,
                archived=False,
                addon_text=ap.addon_text,
            )
            db.add(preset)
            addon_presets_by_id[preset_id] = preset
        else:
            preset.archived = False
            preset.name = ap.addon_preset_name
            preset.addon_text = ap.addon_text

    addon_id_set = set(addon_ids)
    for preset in addon_existing:
        if str(preset.id) not in addon_id_set:
            preset.archived = True

    db.flush()

    # アクティブIDの更新
    global_settings.active_llm_preset_id = request.active_llm_preset_id
    global_settings.active_embedding_preset_id = request.active_embedding_preset_id
    global_settings.active_persona_preset_id = request.active_persona_preset_id
    global_settings.active_addon_preset_id = request.active_addon_preset_id

    active_llm = db.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id, archived=False).first()
    if not active_llm:
        raise HTTPException(status_code=400, detail="Active LLM preset is not set")
    active_embedding = (
        db.query(models.EmbeddingPreset).filter_by(id=global_settings.active_embedding_preset_id, archived=False).first()
    )
    if not active_embedding:
        raise HTTPException(status_code=400, detail="Active embedding preset is not set")
    active_persona = db.query(models.PersonaPreset).filter_by(id=global_settings.active_persona_preset_id, archived=False).first()
    if not active_persona:
        raise HTTPException(status_code=400, detail="Active persona preset is not set")
    active_addon = db.query(models.AddonPreset).filter_by(id=global_settings.active_addon_preset_id, archived=False).first()
    if not active_addon:
        raise HTTPException(status_code=400, detail="Active addon preset is not set")

    # 変更後の設定で RuntimeConfig を組み立て、利用可能なメモリDBかを先に検証する
    runtime_config = build_runtime_config(
        toml_config,
        global_settings,
        active_llm,
        active_embedding,
        active_persona,
        active_addon,
    )
    try:
        init_memory_db(runtime_config.embedding_preset_id, runtime_config.embedding_dimension)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=f"memory DB init failed: {exc}") from exc

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"settings commit failed: {exc.orig}") from exc

    # 設定変更をランタイムへ即時反映
    set_global_config_store(
        ConfigStore(
            toml_config,
            runtime_config,
        )
    )
    reset_memory_manager()

    # 内蔵Workerが有効なら、設定変更に追従させる（LLMプリセット/embedding_preset_id切替など）。
    # 重い処理（join等）になる可能性があるため、レスポンス後に実行する。
    try:
        from cocoro_ghost.internal_worker import request_restart_async

        background_tasks.add_task(
            request_restart_async,
            embedding_preset_id=runtime_config.embedding_preset_id,
            embedding_dimension=runtime_config.embedding_dimension,
        )
    except Exception:  # noqa: BLE001
        pass

    # 最新状態を返す
    return get_settings(db=db)
