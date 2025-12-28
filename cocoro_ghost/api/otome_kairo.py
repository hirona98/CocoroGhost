"""otome_kairo デバッグ用API。

- UI から otome_kairo の数値を参照/変更するための専用API。
- 変更は完全上書きのみ（部分更新は不可）。
- 永続化しない（DBへ保存しない）。
- 認証は main.py 側の router include で強制される想定。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cocoro_ghost.db import get_memory_session
from cocoro_ghost.otome_kairo import EMOTION_LABELS, clamp01, compute_otome_state_from_episodes
from cocoro_ghost.otome_kairo_runtime import (
    apply_otome_state_override,
    get_override,
    get_override_meta,
    set_override,
)
from cocoro_ghost.unit_enums import Sensitivity, UnitKind
from cocoro_ghost.unit_models import Unit


router = APIRouter()


def _now_ts() -> int:
    return int(time.time())


class OtomeKairoRuntimePolicy(BaseModel):
    cooperation: float = Field(ge=0.0, le=1.0)
    refusal_bias: float = Field(ge=0.0, le=1.0)
    refusal_allowed: bool


class OtomeKairoRuntimeComponents(BaseModel):
    joy: float = Field(ge=0.0, le=1.0)
    sadness: float = Field(ge=0.0, le=1.0)
    anger: float = Field(ge=0.0, le=1.0)
    fear: float = Field(ge=0.0, le=1.0)


class OtomeKairoRuntimeRequest(BaseModel):
    # ランタイム状態は完全上書きのみ（部分更新は不可）
    label: str
    intensity: float = Field(ge=0.0, le=1.0)
    components: OtomeKairoRuntimeComponents
    policy: OtomeKairoRuntimePolicy


class OtomeKairoDebugResponse(BaseModel):
    now_ts: int
    runtime_meta: Dict[str, Any]
    runtime_state: Optional[Dict[str, Any]]
    computed: Optional[Dict[str, Any]]
    effective: Dict[str, Any]


def _compute_from_db(*, now_ts: int, scan_limit: int) -> dict[str, Any]:
    """現在のDB状態から otome_state を計算する（永続化はしない）。"""
    # memory_enabled が false の場合でも、DB自体は存在する前提。
    # ただし異常系では例外にして API 側で扱う。
    from cocoro_ghost.config import get_config_store  # noqa: PLC0415

    memory_id = get_config_store().memory_id
    embedding_dimension = int(get_config_store().embedding_dimension)

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
        return compute_otome_state_from_episodes(episodes, now_ts=int(now_ts))
    finally:
        session.close()


@router.get("/otome_kairo", response_model=OtomeKairoDebugResponse)
def get_otome_kairo_debug(scan_limit: int = 500, include_computed: bool = True):
    """現在の otome_kairo（computed / runtime_state / effective）を返す。"""
    now_ts = _now_ts()

    computed: Optional[dict[str, Any]] = None
    if include_computed:
        try:
            scan_limit = max(50, min(2000, int(scan_limit)))
            computed = _compute_from_db(now_ts=now_ts, scan_limit=scan_limit)
        except Exception:  # noqa: BLE001
            # デバッグ用途のため、computed が取れない場合も effective は返す
            computed = None

    effective = apply_otome_state_override(computed, now_ts=now_ts)
    return OtomeKairoDebugResponse(
        now_ts=now_ts,
        runtime_meta=get_override_meta(),
        runtime_state=get_override(),
        computed=computed,
        effective=effective,
    )


@router.put("/otome_kairo", response_model=OtomeKairoDebugResponse)
def put_otome_kairo(request: OtomeKairoRuntimeRequest, scan_limit: int = 500, include_computed: bool = True):
    """otome_kairo のランタイム状態を設定（完全上書きのみ）。"""
    now_ts = _now_ts()

    label = (request.label or "").strip()
    if not label or label not in EMOTION_LABELS:
        raise HTTPException(status_code=400, detail=f"label must be one of: {', '.join(EMOTION_LABELS)}")

    computed: Optional[dict[str, Any]] = None
    if include_computed:
        try:
            scan_limit = max(50, min(2000, int(scan_limit)))
            computed = _compute_from_db(now_ts=now_ts, scan_limit=scan_limit)
        except Exception:  # noqa: BLE001
            computed = None

    # 完全上書きで保存する。
    state: dict[str, Any] = {
        "label": label,
        "intensity": clamp01(request.intensity),
        "components": request.components.model_dump(),
        "policy": request.policy.model_dump(),
    }

    set_override(now_ts=now_ts, state=state)

    effective = apply_otome_state_override(computed, now_ts=now_ts)
    return OtomeKairoDebugResponse(
        now_ts=now_ts,
        runtime_meta=get_override_meta(),
        runtime_state=get_override(),
        computed=computed,
        effective=effective,
    )
