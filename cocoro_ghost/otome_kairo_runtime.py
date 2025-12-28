"""otome_kairo のランタイム上書き（デバッグ用）。

 - UI から otome_kairo 関連の数値を参照/変更できるようにするための in-memory ストア。
 - override は完全上書きのみ（部分マージしない）。
 - 永続化（DB/設定DB/settings）は行わない。
 - FastAPI と internal worker が同一プロセスの場合に有効。
  ※ 複数プロセス/複数ワーカー構成だとプロセスごとに状態が分離される。
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Optional

from cocoro_ghost.otome_kairo import EMOTION_LABELS, clamp01, compute_otome_state_from_episodes


_lock = threading.Lock()
_override: Optional[dict[str, Any]] = None
_override_updated_at: Optional[int] = None

# 「前回チャットで使った値（last used）」を保持する（UI表示用）。
# - override そのものではなく、MemoryPackに注入した otome_state（compact）の最新を保存する。
# - 永続化しない（プロセス再起動で消える）。
_last_used: Optional[dict[str, Any]] = None
_last_used_at: Optional[int] = None


def get_last_used() -> Optional[dict[str, Any]]:
    """前回チャットで使った otome_state（compact）を返す（無ければ None）。"""
    with _lock:
        return copy.deepcopy(_last_used)


def get_last_used_meta() -> dict[str, Any]:
    """last used のメタ情報（デバッグ用）を返す。"""
    with _lock:
        return {
            "enabled": _last_used is not None,
            "updated_at": _last_used_at,
        }


def set_last_used(*, now_ts: int, state: dict[str, Any]) -> dict[str, Any]:
    """前回チャットで使った値（compact）を保存する。

    用途:
    - GET /api/otome_kairo が「前回使った値」を返すため。
    - override をPUTしても、会話が走るまでは last_used は変わらない（=意図通り）。
    """
    if not isinstance(state, dict):
        raise TypeError("state must be dict")

    label = _normalize_label(state.get("label"))
    if label is None:
        raise ValueError(f"label must be one of: {', '.join(EMOTION_LABELS)}")

    intensity_raw = state.get("intensity")
    if intensity_raw is None:
        raise ValueError("intensity is required")

    components = _normalize_components(state.get("components"))
    if components is None:
        raise ValueError("components must include: joy, sadness, anger, fear")

    policy = _normalize_policy(state.get("policy"))
    if policy is None:
        raise ValueError("policy must include: cooperation, refusal_bias, refusal_allowed")

    merged: dict[str, Any] = {
        "label": label,
        "intensity": clamp01(intensity_raw),
        "components": components,
        "policy": policy,
    }

    with _lock:
        global _last_used, _last_used_at
        _last_used = copy.deepcopy(merged)
        _last_used_at = int(now_ts)
        return copy.deepcopy(_last_used)


def get_override() -> Optional[dict[str, Any]]:
    """現在の override を返す（無ければ None）。"""
    with _lock:
        return copy.deepcopy(_override)


def get_override_meta() -> dict[str, Any]:
    """override のメタ情報（デバッグ用）を返す。"""
    with _lock:
        return {
            "enabled": _override is not None,
            "updated_at": _override_updated_at,
        }


def clear_override() -> None:
    """override を解除する。"""
    with _lock:
        global _override, _override_updated_at
        _override = None
        _override_updated_at = None


def _normalize_label(label: Any) -> Optional[str]:
    s = str(label or "").strip()
    return s if s in EMOTION_LABELS else None


def _normalize_components(raw: Any) -> Optional[dict[str, float]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    required = ("joy", "sadness", "anger", "fear")
    if any(k not in raw for k in required):
        return None
    out: dict[str, float] = {}
    for k in required:
        out[k] = clamp01(raw.get(k))
    return out


def _normalize_policy(raw: Any) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    required = ("cooperation", "refusal_bias", "refusal_allowed")
    if any(k not in raw for k in required):
        return None
    out: dict[str, Any] = {}
    out["cooperation"] = clamp01(raw.get("cooperation"))
    out["refusal_bias"] = clamp01(raw.get("refusal_bias"))
    out["refusal_allowed"] = bool(raw.get("refusal_allowed"))
    return out


def set_override(
    *,
    now_ts: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    """override を設定する（完全上書きのみ）。"""
    if not isinstance(state, dict):
        raise TypeError("state must be dict")

    # 完全上書きのみ: 必須キーが揃わなければエラーにする。
    label = _normalize_label(state.get("label"))
    if label is None:
        raise ValueError(f"label must be one of: {', '.join(EMOTION_LABELS)}")

    intensity_raw = state.get("intensity")
    if intensity_raw is None:
        raise ValueError("intensity is required")

    components = _normalize_components(state.get("components"))
    if components is None:
        raise ValueError("components must include: joy, sadness, anger, fear")

    policy = _normalize_policy(state.get("policy"))
    if policy is None:
        raise ValueError("policy must include: cooperation, refusal_bias, refusal_allowed")

    merged: dict[str, Any] = {
        "label": label,
        "intensity": clamp01(intensity_raw),
        "components": components,
        "policy": policy,
    }

    with _lock:
        global _override, _override_updated_at
        _override = copy.deepcopy(merged)
        _override_updated_at = int(now_ts)
        return copy.deepcopy(_override)


def apply_otome_state_override(
    computed_state: Optional[dict[str, Any]],
    *,
    now_ts: int,
) -> dict[str, Any]:
    """計算済み otome_state に override を適用して返す。

    override が無ければ computed_state をそのまま返す。
    """
    override = get_override()
    if override is None:
        return computed_state if isinstance(computed_state, dict) else compute_otome_state_from_episodes([], now_ts=int(now_ts))

    base = computed_state if isinstance(computed_state, dict) else compute_otome_state_from_episodes([], now_ts=int(now_ts))
    out = copy.deepcopy(base)
    # 完全上書き: components/policy も含めて差し替える。
    out["label"] = override["label"]
    out["intensity"] = clamp01(override["intensity"])
    out["components"] = copy.deepcopy(override["components"])
    out["policy"] = copy.deepcopy(override["policy"])
    out["now_ts"] = int(now_ts)
    return out
