"""感情（otome_kairo）計算ユーティリティ。

このモジュールは「会話本文に“感情の反映”を入れる」ための中核ロジックです。

ここでいう感情は、次をまとめて扱う仕組みを指します。

- 即時性（“その発言で怒る”）: /api/chat の同一LLM出力に埋め込んだ内部JSON（reflection）で
  Unit.emotion_* を即時更新する。
- 持続性（“大事件の余韻が残る”）: 過去エピソードの影響を「重要度×時間減衰」で積分し、
    現在の状態（otome_state）を推定する。

"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional


# /api/chat（SSE）で返答本文の末尾に付与する区切り文字。
# ここより後ろのJSONはサーバ側で回収し、SSEには流さない。
OTOME_KAIRO_TRAILER_MARKER = "<<<COCORO_GHOST_OTOME_KAIRO_JSON_v1>>>"

# 内部JSON（reflection）で使う感情ラベル（喜怒哀楽 + neutral）。
EMOTION_LABELS = ("joy", "sadness", "anger", "fear", "neutral")


def _normalize_partner_policy(raw: object) -> dict | None:
    """LLM出力の partner_policy を内部policy形式へ正規化する（不正値はNone）。"""
    if not isinstance(raw, dict):
        return None
    required = ("cooperation", "refusal_bias", "refusal_allowed")
    if any(k not in raw for k in required):
        return None
    try:
        cooperation = clamp01(raw.get("cooperation"))
        refusal_bias = clamp01(raw.get("refusal_bias"))
        refusal_allowed = bool(raw.get("refusal_allowed"))
    except Exception:  # noqa: BLE001
        return None
    return {
        "cooperation": cooperation,
        "refusal_bias": refusal_bias,
        "refusal_allowed": refusal_allowed,
    }


def clamp01(x: float) -> float:
    """0..1 にクランプする（不正値は0扱い）。"""
    try:
        x = float(x)
    except Exception:  # noqa: BLE001
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass(frozen=True)
class OtomeKairoDecayParams:
    """salienceに応じて残留時間を変えるためのパラメータ。"""

    tau_min_seconds: float = 120.0
    tau_max_seconds: float = 6 * 3600.0
    salience_power: float = 2.0


def tau_from_salience(salience: float, *, params: OtomeKairoDecayParams) -> float:
    """salience（重要度）から残留時間 τ（秒）を決める。"""
    s = clamp01(salience)
    tau_min = max(1.0, float(params.tau_min_seconds))
    tau_max = max(tau_min, float(params.tau_max_seconds))
    k = max(0.1, float(params.salience_power))
    return tau_min + (tau_max - tau_min) * (s**k)


def decay_weight(*, dt_seconds: float, tau_seconds: float) -> float:
    """時間減衰の重み（0..1）を返す。 w = exp(-Δt / τ)"""
    if tau_seconds <= 0:
        return 0.0
    dt = max(0.0, float(dt_seconds))
    return float(math.exp(-dt / float(tau_seconds)))


def compress_sum_to_01(x: float) -> float:
    """0..∞ の和を 0..1 に圧縮する（単調増加・飽和）。 y = 1 - exp(-x)"""
    x = max(0.0, float(x))
    return float(1.0 - math.exp(-x))


def compute_otome_state_from_episodes(
    episodes: Iterable[dict],
    *,
    now_ts: int,
    params: OtomeKairoDecayParams | None = None,
) -> dict:
    """Episode列（dict）から感情（otome_state）を推定する。

    episodes の要素は最低限:
      occurred_at(int|None), created_at(int|None),
      emotion_label(str|None), emotion_intensity(float|None),
      salience(float|None), confidence(float|None)
    """
    p = params or OtomeKairoDecayParams()
    sums: Dict[str, float] = {k: 0.0 for k in EMOTION_LABELS}

    # partner_policy（行動方針ノブ）は、最新の強い出来事を優先して状態へ反映する。
    # - /api/chat の内部JSONで「その瞬間の態度」を出せても、積分ロジックだけだと反映されないため。
    # - 強さは salience×confidence×時間減衰 で評価する（感情強度とは独立）。
    policy_candidate: tuple[float, dict] | None = None

    for e in episodes:
        label = str(e.get("emotion_label") or "").strip()
        if label not in EMOTION_LABELS:
            # 感情ラベルが無くても、partner_policy だけは拾う。
            label = ""

        intensity = clamp01(e.get("emotion_intensity") or 0.0)
        salience = clamp01(e.get("salience") or 0.0)
        confidence = clamp01(e.get("confidence") or 0.5)

        occurred_at = e.get("occurred_at")
        created_at = e.get("created_at")
        base_ts: Optional[int] = None
        if isinstance(occurred_at, int):
            base_ts = occurred_at
        elif isinstance(created_at, int):
            base_ts = created_at
        else:
            base_ts = int(now_ts)

        dt = max(0, int(now_ts) - int(base_ts))
        tau = tau_from_salience(salience, params=p)
        w = decay_weight(dt_seconds=dt, tau_seconds=tau)

        # partner_policy は「最新で重要なもの」を優先し、徐々に薄れる。
        # ここでは単純に最大スコアのものを採用する（複数混ぜるより暴れにくい）。
        raw_policy = e.get("partner_policy")
        policy = _normalize_partner_policy(raw_policy)
        if policy is not None:
            score = clamp01(1.5 * salience * confidence * w)
            if score > 0.0 and (policy_candidate is None or score > policy_candidate[0]):
                policy_candidate = (score, policy)

        # 感情の積分（labelが無い/neutralは無視）
        if label and label in EMOTION_LABELS:
            if intensity <= 0.0 or salience <= 0.0:
                continue
            impact = float(intensity * salience * confidence * w)
            sums[label] += impact

    # neutral は採点に使わない（“何もないのに neutral が強い”を避ける）
    comps = {
        "joy": compress_sum_to_01(sums["joy"]),
        "sadness": compress_sum_to_01(sums["sadness"]),
        "anger": compress_sum_to_01(sums["anger"]),
        "fear": compress_sum_to_01(sums["fear"]),
    }

    dominant = max(comps.items(), key=lambda kv: kv[1])[0] if comps else "neutral"
    intensity = max(comps.values()) if comps else 0.0
    # ほぼ無風なら neutral に戻す（微小なノイズで揺れないようにする）
    if intensity < 0.15:
        dominant = "neutral"

    # 行動方針ノブ（policy）
    anger = float(comps.get("anger") or 0.0)
    refusal_bias = clamp01((anger - 0.55) / 0.45)
    cooperation = clamp01(1.0 - 0.9 * refusal_bias)
    refusal_allowed = bool(anger >= 0.75)

    policy_obj = {
        "cooperation": cooperation,
        "refusal_bias": refusal_bias,
        "refusal_allowed": refusal_allowed,
    }

    # partner_policy がある場合はブレンドする。
    # - 数値は線形補間（0..1）
    # - boolは重みが強いときのみ上書き（弱いときは標準ロジックを優先）
    if policy_candidate is not None:
        weight, override_policy = policy_candidate
        weight = clamp01(weight)
        try:
            policy_obj["cooperation"] = clamp01(
                (1.0 - weight) * float(policy_obj["cooperation"]) + weight * float(override_policy["cooperation"])
            )
            policy_obj["refusal_bias"] = clamp01(
                (1.0 - weight) * float(policy_obj["refusal_bias"]) + weight * float(override_policy["refusal_bias"])
            )
            if weight >= 0.5:
                policy_obj["refusal_allowed"] = bool(override_policy["refusal_allowed"])
        except Exception:  # noqa: BLE001
            pass

    return {
        "schema": "otome_state_v1",
        "now_ts": int(now_ts),
        "label": dominant,
        "intensity": clamp01(intensity),
        "components": {k: clamp01(v) for k, v in comps.items()},
        "policy": policy_obj,
        "params": {
            "tau_min_seconds": float(p.tau_min_seconds),
            "tau_max_seconds": float(p.tau_max_seconds),
            "salience_power": float(p.salience_power),
        },
    }
