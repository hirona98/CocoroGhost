"""
AI人格の機嫌（persona_mood）計算ユーティリティ

会話における「機嫌の反映」を実現するための中核ロジック。

ここでいう機嫌は2つの性質を持つ:
- 即時性: /api/chat の内部JSON（reflection）でpersona_affect_*を即時更新
- 持続性: 過去エピソードの影響を「重要度×時間減衰」で積分し、現在状態を推定

計算式: impact = intensity × salience × confidence × exp(-Δt/τ)
- salienceが高いほどτが長く、大事件の余韻が長く残る
- salienceが低いほどτが短く、雑談はすぐに消える
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional


# /api/chat（SSE）で返答本文の末尾に付与する区切り文字。
# ここより後ろのJSONはサーバ側で回収し、SSEには流さない。
PERSONA_AFFECT_TRAILER_MARKER = "<<<COCORO_GHOST_PERSONA_AFFECT_JSON_v1>>>"

# persona_mood_state のラベル（喜怒哀楽 + neutral）。
PERSONA_MOOD_LABELS = ("joy", "sadness", "anger", "fear", "neutral")


def _normalize_persona_response_policy(raw: object) -> dict | None:
    """
    persona_response_policyを正規化する。

    LLM出力を内部形式に変換し、不正値はNoneを返す。
    """
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
    """
    値を0..1の範囲にクランプする。

    変換できない不正値は0として扱う。
    """
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
class PersonaMoodDecayParams:
    """
    時間減衰パラメータ。

    salienceに応じて残留時間τを変化させる。
    """

    tau_min_seconds: float = 120.0
    tau_max_seconds: float = 6 * 3600.0
    salience_power: float = 2.0


def tau_from_salience(salience: float, *, params: PersonaMoodDecayParams) -> float:
    """
    salienceから残留時間τを計算する。

    重要度が高いほど長時間残り、低いほど早く消える。
    """
    s = clamp01(salience)
    tau_min = max(1.0, float(params.tau_min_seconds))
    tau_max = max(tau_min, float(params.tau_max_seconds))
    k = max(0.1, float(params.salience_power))
    return tau_min + (tau_max - tau_min) * (s**k)


def decay_weight(*, dt_seconds: float, tau_seconds: float) -> float:
    """
    時間減衰の重みを計算する。

    指数減衰 w = exp(-Δt / τ) で0..1の値を返す。
    """
    if tau_seconds <= 0:
        return 0.0
    dt = max(0.0, float(dt_seconds))
    return float(math.exp(-dt / float(tau_seconds)))


def compress_sum_to_01(x: float) -> float:
    """
    累積値を0..1に圧縮する。

    y = 1 - exp(-x) で単調増加し、飽和する。
    """
    x = max(0.0, float(x))
    return float(1.0 - math.exp(-x))


def compute_persona_mood_state_from_episodes(
    episodes: Iterable[dict],
    *,
    now_ts: int,
    params: PersonaMoodDecayParams | None = None,
) -> dict:
    """
    エピソード列からpersona_mood_stateを推定する。

    各エピソードの感情ラベル・強度・重要度・時間を考慮し、
    現在の感情状態と行動方針を計算する。
    episodesには occurred_at, created_at, persona_affect_label,
    persona_affect_intensity, salience, confidence が必要。
    """
    p = params or PersonaMoodDecayParams()
    sums: Dict[str, float] = {k: 0.0 for k in PERSONA_MOOD_LABELS}

    # persona_response_policy（行動方針ノブ）は、最新の強い出来事を優先して状態へ反映する。
    # - /api/chat の内部JSONで「その瞬間の態度」を出せても、積分ロジックだけだと反映されないため。
    # - 強さは salience×confidence×時間減衰 で評価する（感情強度とは独立）。
    policy_candidate: tuple[float, dict] | None = None

    for e in episodes:
        label = str(e.get("persona_affect_label") or "").strip()
        if label not in PERSONA_MOOD_LABELS:
            # 感情ラベルが無くても、persona_response_policy だけは拾う。
            label = ""

        intensity = clamp01(e.get("persona_affect_intensity") or 0.0)
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

        # persona_response_policy は「最新で重要なもの」を優先し、徐々に薄れる。
        # ここでは単純に最大スコアのものを採用する（複数混ぜるより暴れにくい）。
        raw_policy = e.get("persona_response_policy")
        policy = _normalize_persona_response_policy(raw_policy)
        if policy is not None:
            score = clamp01(1.5 * salience * confidence * w)
            if score > 0.0 and (policy_candidate is None or score > policy_candidate[0]):
                policy_candidate = (score, policy)

        # moodの積分（labelが無い/neutralは無視）
        if label and label in PERSONA_MOOD_LABELS:
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

    # 行動方針ノブ（response_policy）
    anger = float(comps.get("anger") or 0.0)
    refusal_bias = clamp01((anger - 0.55) / 0.45)
    cooperation = clamp01(1.0 - 0.9 * refusal_bias)
    refusal_allowed = bool(anger >= 0.75)

    response_policy_obj = {
        "cooperation": cooperation,
        "refusal_bias": refusal_bias,
        "refusal_allowed": refusal_allowed,
    }

    # persona_response_policy がある場合はブレンドする。
    # - 数値は線形補間（0..1）
    # - boolは重みが強いときのみ上書き（弱いときは標準ロジックを優先）
    if policy_candidate is not None:
        weight, override_policy = policy_candidate
        weight = clamp01(weight)
        try:
            response_policy_obj["cooperation"] = clamp01(
                (1.0 - weight) * float(response_policy_obj["cooperation"]) + weight * float(override_policy["cooperation"])
            )
            response_policy_obj["refusal_bias"] = clamp01(
                (1.0 - weight) * float(response_policy_obj["refusal_bias"]) + weight * float(override_policy["refusal_bias"])
            )
            if weight >= 0.5:
                response_policy_obj["refusal_allowed"] = bool(override_policy["refusal_allowed"])
        except Exception:  # noqa: BLE001
            pass

    return {
        "schema": "persona_mood_state_v2",
        "now_ts": int(now_ts),
        "label": dominant,
        "intensity": clamp01(intensity),
        "components": {k: clamp01(v) for k, v in comps.items()},
        "response_policy": response_policy_obj,
        "params": {
            "tau_min_seconds": float(p.tau_min_seconds),
            "tau_max_seconds": float(p.tau_max_seconds),
            "salience_power": float(p.salience_power),
        },
    }
