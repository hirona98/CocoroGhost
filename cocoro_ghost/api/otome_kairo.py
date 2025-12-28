"""otome_kairo デバッグ用API。

- UI から otome_kairo（パートナーの感情）を参照/変更するための専用API。
- このAPIで扱うのは「次のチャットでLLMに渡す予定の値」= 現在有効な値のみ。
- 変更は完全上書きのみ（部分更新は不可）。
- 永続化しない（DBへ保存しない）。
- override 未設定時は、常にデフォルト値（neutral）を返す（DB計算はしない）。
- 認証は main.py 側の router include で強制される想定。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cocoro_ghost.otome_kairo import EMOTION_LABELS, clamp01
from cocoro_ghost.otome_kairo_runtime import clear_override, get_last_used, set_override



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


class OtomeKairoState(BaseModel):
    """otome_kairo の状態（APIのRequest/Response共通）。

    - 完全上書きのみ（部分更新は不可）
    """

    label: str
    intensity: float = Field(ge=0.0, le=1.0)
    components: OtomeKairoRuntimeComponents
    policy: OtomeKairoRuntimePolicy


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
        "policy": {"cooperation": 1.0, "refusal_bias": 0.0, "refusal_allowed": False},
    }


@router.get("/otome_kairo", response_model=OtomeKairoState)
def get_otome_kairo():
    """otome_kairo の「前回チャットで使った値」を返す（無ければデフォルト）。"""
    now_ts = _now_ts()
    effective = _get_last_used_or_default(now_ts=now_ts)
    return OtomeKairoState(**effective)


@router.put("/otome_kairo", response_model=OtomeKairoState)
def put_otome_kairo(request: OtomeKairoState):
    """otome_kairo のランタイム状態を設定（完全上書きのみ）。"""
    now_ts = _now_ts()

    label = (request.label or "").strip()
    if not label or label not in EMOTION_LABELS:
        raise HTTPException(status_code=400, detail=f"label must be one of: {', '.join(EMOTION_LABELS)}")

    # 完全上書きで保存する（次のチャットで有効な値）。
    state: dict[str, Any] = {
        "label": label,
        "intensity": clamp01(request.intensity),
        "components": request.components.model_dump(),
        "policy": request.policy.model_dump(),
    }
    set_override(now_ts=now_ts, state=state)
    return OtomeKairoState(**state)


@router.delete("/otome_kairo", response_model=OtomeKairoState)
def delete_otome_kairo_override():
    """otome_kairo の in-memory override を解除する。

    解除後は、次のチャットで使われる値はDBからの自然計算に戻る。
    """
    now_ts = _now_ts()
    # 手動操作モードを明示的に解除するためのAPI。
    clear_override()
    # 解除しても「前回使った値」は変わらないため、GET相当を返す。
    effective = _get_last_used_or_default(now_ts=now_ts)
    return OtomeKairoState(**effective)
