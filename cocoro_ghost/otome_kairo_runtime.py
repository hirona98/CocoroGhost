"""otome_kairo のランタイム上書き（デバッグ用）。

- UI から otome_kairo 関連の数値を参照/変更できるようにするための in-memory ストア。
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
    out: dict[str, float] = {}
    for k in ("joy", "sadness", "anger", "fear"):
        if k in raw:
            out[k] = clamp01(raw.get(k))
    return out


def _normalize_policy(raw: Any) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    if "cooperation" in raw:
        out["cooperation"] = clamp01(raw.get("cooperation"))
    if "refusal_bias" in raw:
        out["refusal_bias"] = clamp01(raw.get("refusal_bias"))
    if "refusal_allowed" in raw:
        out["refusal_allowed"] = bool(raw.get("refusal_allowed"))
    return out


def set_override(
    *,
    now_ts: int,
    patch: dict[str, Any],
    base: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """override を設定する。

    patch は部分指定を許容する。
    - base があればそれを土台に merge
    - base が無ければ neutral ベースを土台に merge

    返り値は「保存された override（deep copy）」。
    """
    if not isinstance(patch, dict):
        raise TypeError("patch must be dict")

    base_state = base if isinstance(base, dict) else compute_otome_state_from_episodes([], now_ts=int(now_ts))

    label = _normalize_label(patch.get("label"))
    intensity = patch.get("intensity")
    components = _normalize_components(patch.get("components"))
    policy = _normalize_policy(patch.get("policy"))

    # 保存形式は「otome_state の一部」を上書きするパッチとして保持する。
    merged: dict[str, Any] = {
        "label": label if label is not None else base_state.get("label"),
        "intensity": clamp01(float(intensity)) if intensity is not None else base_state.get("intensity"),
    }

    if components is not None:
        merged["components"] = components
    if policy is not None:
        merged["policy"] = policy

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

    if isinstance(override.get("label"), str):
        out["label"] = override["label"]
    if override.get("intensity") is not None:
        out["intensity"] = clamp01(override.get("intensity"))

    # components/policy は部分上書き
    if isinstance(override.get("components"), dict):
        comps = dict(out.get("components") or {})
        for k, v in override["components"].items():
            comps[str(k)] = clamp01(v)
        out["components"] = comps

    if isinstance(override.get("policy"), dict):
        pol = dict(out.get("policy") or {})
        for k, v in override["policy"].items():
            if str(k) == "refusal_allowed":
                pol["refusal_allowed"] = bool(v)
            else:
                pol[str(k)] = clamp01(v)
        out["policy"] = pol

    out["now_ts"] = int(now_ts)
    return out
