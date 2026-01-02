"""partner_mood デバッグ用API。

- UI から partner_mood（AI人格の機嫌）を参照/変更するための専用API。
- このAPIで扱うのは「次のチャットでLLMに渡す予定の値」= 現在有効な値のみ。
- 変更は完全上書きのみ（部分更新は不可）。
- 永続化しない（DBへ保存しない）。
- override 未設定時は、常にデフォルト値（neutral）を返す（DB計算はしない）。
- 認証は main.py 側の router include で強制される想定。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cocoro_ghost.partner_mood import PARTNER_MOOD_LABELS, clamp01
from cocoro_ghost.partner_mood_runtime import clear_override, get_last_used, set_override


router = APIRouter()


def _now_ts() -> int:
    return int(time.time())


class PartnerMoodRuntimePolicy(BaseModel):
    cooperation: float = Field(ge=0.0, le=1.0)
    refusal_bias: float = Field(ge=0.0, le=1.0)
    refusal_allowed: bool


class PartnerMoodRuntimeComponents(BaseModel):
    joy: float = Field(ge=0.0, le=1.0)
    sadness: float = Field(ge=0.0, le=1.0)
    anger: float = Field(ge=0.0, le=1.0)
    fear: float = Field(ge=0.0, le=1.0)


class PartnerMoodState(BaseModel):
    """partner_mood の状態（APIのRequest/Response共通）。

    - 完全上書きのみ（部分更新は不可）
    """

    label: str
    intensity: float = Field(ge=0.0, le=1.0)
    components: PartnerMoodRuntimeComponents
    response_policy: PartnerMoodRuntimePolicy


def _get_last_used_or_default(*, now_ts: int) -> dict[str, Any]:
    """API用の状態を返す。

    - last used（前回チャットで使った値）があればそれを返す
    - 無ければデフォルト値（neutral）を返す
    """
    last_used = get_last_used()
    if isinstance(last_used, dict):
        return last_used

    return {
        "label": "neutral",
        "intensity": 0.0,
        "components": {"joy": 0.0, "sadness": 0.0, "anger": 0.0, "fear": 0.0},
        "response_policy": {"cooperation": 1.0, "refusal_bias": 0.0, "refusal_allowed": False},
    }


@router.get("/partner_mood", response_model=PartnerMoodState)
def get_partner_mood():
    """partner_mood の「前回チャットで使った値」を返す（無ければデフォルト）。"""
    now_ts = _now_ts()
    effective = _get_last_used_or_default(now_ts=now_ts)
    return PartnerMoodState(**effective)


@router.put("/partner_mood", response_model=PartnerMoodState)
def put_partner_mood(request: PartnerMoodState):
    """partner_mood のランタイム状態を設定（完全上書きのみ）。"""
    now_ts = _now_ts()

    label = (request.label or "").strip()
    if not label or label not in PARTNER_MOOD_LABELS:
        raise HTTPException(status_code=400, detail=f"label must be one of: {', '.join(PARTNER_MOOD_LABELS)}")

    # 完全上書きで保存する（次のチャットで有効な値）。
    state: dict[str, Any] = {
        "label": label,
        "intensity": clamp01(request.intensity),
        "components": request.components.model_dump(),
        "response_policy": request.response_policy.model_dump(),
    }
    set_override(now_ts=now_ts, state=state)
    return PartnerMoodState(**state)


@router.delete("/partner_mood", response_model=PartnerMoodState)
def delete_partner_mood_override():
    """partner_mood の in-memory override を解除する。

    解除しても「前回使った値」は変わらないため、GET相当を返す。
    """
    now_ts = _now_ts()
    clear_override()
    effective = _get_last_used_or_default(now_ts=now_ts)
    return PartnerMoodState(**effective)
