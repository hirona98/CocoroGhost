"""
管理API

提供する機能:
- 出来事ログ（events）の閲覧
- 状態（state）の閲覧
- 改訂履歴（revisions）の閲覧
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from cocoro_ghost import models
from cocoro_ghost.db import memory_session_scope, settings_session_scope
from cocoro_ghost.memory_models import Event, Revision, State


router = APIRouter()


def _resolve_embedding_dimension(embedding_preset_id: str) -> int:
    """embedding_preset_id に対応する埋め込み次元を settings.db から解決する。"""

    # --- 入力チェック ---
    pid = (embedding_preset_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="embedding_preset_id is required")

    # --- settings.db を参照 ---
    with settings_session_scope() as session:
        preset = session.query(models.EmbeddingPreset).filter_by(id=pid, archived=False).first()
        if preset is None:
            raise HTTPException(status_code=400, detail="invalid embedding_preset_id")
        return int(preset.embedding_dimension)


# --- events ---


@router.get("/memories/{embedding_preset_id}/events")
def list_events(
    embedding_preset_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    source: Optional[str] = Query(default=None),
):
    """出来事ログ（events）の一覧を返す。"""

    # --- DBを開く（embedding_preset_idごとに分離） ---
    embedding_dimension = _resolve_embedding_dimension(embedding_preset_id)
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- フィルタを組み立てる ---
        q = db.query(Event)
        if source is not None:
            s = str(source or "").strip()
            if s:
                q = q.filter(Event.source == s)

        # --- 取得 ---
        rows = q.order_by(Event.created_at.desc(), Event.event_id.desc()).offset(int(offset)).limit(int(limit)).all()

        # --- 応答 ---
        return {
            "items": [
                {
                    "event_id": int(r.event_id),
                    "created_at": int(r.created_at),
                    "updated_at": int(r.updated_at),
                    "client_id": r.client_id,
                    "source": str(r.source),
                    "user_text": r.user_text,
                    "assistant_text": r.assistant_text,
                    "about_time": {
                        "about_start_ts": r.about_start_ts,
                        "about_end_ts": r.about_end_ts,
                        "about_year_start": r.about_year_start,
                        "about_year_end": r.about_year_end,
                        "life_stage": r.life_stage,
                        "about_time_confidence": float(r.about_time_confidence),
                    },
                    "entities_json": r.entities_json,
                }
                for r in rows
            ]
        }


@router.get("/memories/{embedding_preset_id}/events/{event_id}")
def get_event(
    embedding_preset_id: str,
    event_id: int,
):
    """出来事ログ（events）の詳細を返す。"""

    # --- DBを開く ---
    embedding_dimension = _resolve_embedding_dimension(embedding_preset_id)
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 取得 ---
        r = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail="event not found")

        # --- 応答 ---
        return {
            "event_id": int(r.event_id),
            "created_at": int(r.created_at),
            "updated_at": int(r.updated_at),
            "client_id": r.client_id,
            "source": str(r.source),
            "user_text": r.user_text,
            "assistant_text": r.assistant_text,
            "about_time": {
                "about_start_ts": r.about_start_ts,
                "about_end_ts": r.about_end_ts,
                "about_year_start": r.about_year_start,
                "about_year_end": r.about_year_end,
                "life_stage": r.life_stage,
                "about_time_confidence": float(r.about_time_confidence),
            },
            "entities_json": r.entities_json,
            "client_context_json": r.client_context_json,
        }


# --- state ---


@router.get("/memories/{embedding_preset_id}/state")
def list_state(
    embedding_preset_id: str,
    kind: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """状態（state）の一覧を返す。"""

    # --- DBを開く ---
    embedding_dimension = _resolve_embedding_dimension(embedding_preset_id)
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- フィルタ ---
        q = db.query(State)
        if kind is not None:
            k = str(kind or "").strip()
            if k:
                q = q.filter(State.kind == k)

        # --- 取得 ---
        rows = q.order_by(State.last_confirmed_at.desc(), State.state_id.desc()).offset(int(offset)).limit(int(limit)).all()

        # --- 応答 ---
        return {
            "items": [
                {
                    "state_id": int(r.state_id),
                    "kind": str(r.kind),
                    "body_text": str(r.body_text),
                    "payload_json": r.payload_json,
                    "last_confirmed_at": int(r.last_confirmed_at),
                    "confidence": float(r.confidence),
                    "salience": float(r.salience),
                    "valid_from_ts": r.valid_from_ts,
                    "valid_to_ts": r.valid_to_ts,
                    "created_at": int(r.created_at),
                    "updated_at": int(r.updated_at),
                }
                for r in rows
            ]
        }


@router.get("/memories/{embedding_preset_id}/state/{kind}/{state_id}")
def get_state(
    embedding_preset_id: str,
    kind: str,
    state_id: int,
):
    """状態（state）の詳細を返す。"""

    # --- 入力を正規化 ---
    k = str(kind or "").strip()
    if not k:
        raise HTTPException(status_code=400, detail="kind is required")

    # --- DBを開く ---
    embedding_dimension = _resolve_embedding_dimension(embedding_preset_id)
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 取得 ---
        r = db.query(State).filter(State.state_id == int(state_id)).one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail="state not found")
        if str(r.kind) != k:
            raise HTTPException(status_code=404, detail="state not found")

        # --- 応答 ---
        return {
            "state_id": int(r.state_id),
            "kind": str(r.kind),
            "body_text": str(r.body_text),
            "payload_json": r.payload_json,
            "last_confirmed_at": int(r.last_confirmed_at),
            "confidence": float(r.confidence),
            "salience": float(r.salience),
            "valid_from_ts": r.valid_from_ts,
            "valid_to_ts": r.valid_to_ts,
            "created_at": int(r.created_at),
            "updated_at": int(r.updated_at),
        }


@router.get("/memories/{embedding_preset_id}/state/{kind}/{state_id}/revisions")
def list_state_revisions(
    embedding_preset_id: str,
    kind: str,
    state_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """状態（state）の改訂履歴（revisions）を返す。"""

    # --- kindが一致するstateのみ対象にする ---
    _ = get_state(embedding_preset_id=embedding_preset_id, kind=kind, state_id=state_id)

    # --- DBを開く ---
    embedding_dimension = _resolve_embedding_dimension(embedding_preset_id)
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 取得 ---
        q = (
            db.query(Revision)
            .filter(Revision.entity_type == "state")
            .filter(Revision.entity_id == int(state_id))
            .order_by(Revision.created_at.desc(), Revision.revision_id.desc())
        )
        rows = q.offset(int(offset)).limit(int(limit)).all()

        # --- 応答 ---
        return {
            "items": [
                {
                    "revision_id": int(r.revision_id),
                    "entity_type": str(r.entity_type),
                    "entity_id": int(r.entity_id),
                    "before_json": r.before_json,
                    "after_json": r.after_json,
                    "reason": str(r.reason),
                    "evidence_event_ids_json": r.evidence_event_ids_json,
                    "created_at": int(r.created_at),
                }
                for r in rows
            ]
        }

