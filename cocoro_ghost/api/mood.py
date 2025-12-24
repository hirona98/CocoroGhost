"""mood デバッグ用API。

- UI から mood の数値を参照/変更するための専用API。
- 永続化しない（DBへ保存しない）。
- 認証は main.py 側の router include で強制される想定。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import get_memory_session
from cocoro_ghost.mood import EMOTION_LABELS, clamp01, compute_partner_mood_from_episodes
from cocoro_ghost.mood_runtime import apply_partner_mood_override, clear_override, get_override, get_override_meta, set_override
from cocoro_ghost.unit_enums import Sensitivity, UnitKind
from cocoro_ghost.unit_models import Unit


router = APIRouter()


def _now_ts() -> int:
    return int(time.time())


class MoodOverridePolicy(BaseModel):
    cooperation: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    refusal_bias: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    refusal_allowed: Optional[bool] = None


class MoodOverrideComponents(BaseModel):
    joy: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sadness: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    anger: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    fear: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class MoodOverrideRequest(BaseModel):
    # 部分更新を許容する（デバッグUIでスライダー等を想定）
    label: Optional[str] = None
    intensity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    components: Optional[MoodOverrideComponents] = None
    policy: Optional[MoodOverridePolicy] = None


class MoodDebugResponse(BaseModel):
    now_ts: int
    override_meta: Dict[str, Any]
    override: Optional[Dict[str, Any]]
    computed: Optional[Dict[str, Any]]
    effective: Dict[str, Any]


def _compute_from_db(*, now_ts: int, scan_limit: int) -> dict[str, Any]:
    """現在のDB状態から partner_mood を計算する（永続化はしない）。"""
    cfg = get_config_store().config
    memory_id = get_config_store().memory_id
    embedding_dimension = int(get_config_store().embedding_dimension)

    # memory_enabled が false の場合でも、DB自体は存在する前提。
    # ただし異常系では例外にして API 側で扱う。
    session = get_memory_session(str(memory_id), int(embedding_dimension))
    try:
        rows = (
            session.query(Unit)
            .filter(
                Unit.kind == int(UnitKind.EPISODE),
                Unit.state.in_([0, 1, 2]),
                Unit.sensitivity <= int(Sensitivity.SECRET),
                Unit.emotion_label.isnot(None),
            )
            .order_by(Unit.created_at.desc(), Unit.id.desc())
            .limit(int(scan_limit))
            .all()
        )
        episodes = []
        for u in rows:
            episodes.append(
                {
                    "occurred_at": int(u.occurred_at) if u.occurred_at is not None else None,
                    "created_at": int(u.created_at),
                    "emotion_label": u.emotion_label,
                    "emotion_intensity": u.emotion_intensity,
                    "salience": u.salience,
                    "confidence": u.confidence,
                }
            )
        return compute_partner_mood_from_episodes(episodes, now_ts=int(now_ts))
    finally:
        session.close()


@router.get("/mood", response_model=MoodDebugResponse)
def get_mood_debug(scan_limit: int = 500, include_computed: bool = True):
    """現在の mood（computed / override / effective）を返す。"""
    now_ts = _now_ts()

    computed: Optional[dict[str, Any]] = None
    if include_computed:
        try:
            scan_limit = max(50, min(2000, int(scan_limit)))
            computed = _compute_from_db(now_ts=now_ts, scan_limit=scan_limit)
        except Exception as exc:  # noqa: BLE001
            # デバッグ用途のため、computed が取れない場合も effective は返す
            computed = None

    effective = apply_partner_mood_override(computed, now_ts=now_ts)
    return MoodDebugResponse(
        now_ts=now_ts,
        override_meta=get_override_meta(),
        override=get_override(),
        computed=computed,
        effective=effective,
    )


@router.put("/mood/override", response_model=MoodDebugResponse)
def put_mood_override(request: MoodOverrideRequest, scan_limit: int = 500, include_computed: bool = True):
    """mood override を設定（部分更新可）。"""
    now_ts = _now_ts()

    label = (request.label or "").strip() if request.label is not None else None
    if label is not None and label and label not in EMOTION_LABELS:
        raise HTTPException(status_code=400, detail=f"label must be one of: {', '.join(EMOTION_LABELS)}")

    computed: Optional[dict[str, Any]] = None
    if include_computed:
        try:
            scan_limit = max(50, min(2000, int(scan_limit)))
            computed = _compute_from_db(now_ts=now_ts, scan_limit=scan_limit)
        except Exception:  # noqa: BLE001
            computed = None

    patch: dict[str, Any] = {}
    if request.label is not None:
        patch["label"] = label
    if request.intensity is not None:
        patch["intensity"] = clamp01(request.intensity)
    if request.components is not None:
        patch["components"] = {k: v for k, v in request.components.model_dump().items() if v is not None}
    if request.policy is not None:
        patch["policy"] = {k: v for k, v in request.policy.model_dump().items() if v is not None}

    set_override(now_ts=now_ts, patch=patch, base=computed)

    effective = apply_partner_mood_override(computed, now_ts=now_ts)
    return MoodDebugResponse(
        now_ts=now_ts,
        override_meta=get_override_meta(),
        override=get_override(),
        computed=computed,
        effective=effective,
    )


@router.delete("/mood/override", response_model=MoodDebugResponse)
def delete_mood_override(scan_limit: int = 500, include_computed: bool = True):
    """mood override を解除する。"""
    now_ts = _now_ts()
    clear_override()

    computed: Optional[dict[str, Any]] = None
    if include_computed:
        try:
            scan_limit = max(50, min(2000, int(scan_limit)))
            computed = _compute_from_db(now_ts=now_ts, scan_limit=scan_limit)
        except Exception:  # noqa: BLE001
            computed = None

    effective = apply_partner_mood_override(computed, now_ts=now_ts)
    return MoodDebugResponse(
        now_ts=now_ts,
        override_meta=get_override_meta(),
        override=get_override(),
        computed=computed,
        effective=effective,
    )
