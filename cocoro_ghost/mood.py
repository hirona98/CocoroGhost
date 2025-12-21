"""
パートナーAIの機嫌（mood）計算ユーティリティ。

このモジュールは「会話本文に機嫌（喜怒哀楽）を反映させる」ための中核ロジックです。

設計意図
--------
1) 即時性（“その発言で怒る”）と、持続性（“大事件の余韻が残る”）を両立する
   - 即時性: /api/chat の同一LLM出力内に埋め込んだ内部JSON（reflection）で Unit.emotion_* を即時更新する。
   - 持続性: 過去エピソードの影響を「重要度×時間減衰」で積分し、現在の気分を推定する。

2) 「直近N件」依存を避ける
   - 直近N件の単純平均だと、非常に印象的な出来事（salienceが高い）が数ターンで埋もれて消える。
   - そこで、各エピソード i の影響度を次で定義する:

       impact_i = emotion_intensity_i × salience_i × confidence_i × exp(-Δt / τ_i)

     - emotion_intensity: その瞬間の感情の強さ（0..1）
     - salience: 出来事の重要度（0..1）
     - confidence: 推定の確からしさ（0..1）
     - Δt: 今から見た経過秒
     - τ_i: 残留時間（秒）。salience が高いほど長く残るように可変にする

3) 機嫌を「口調だけ」に閉じず、行動方針にも反映できる形にする
   - ここでは anger 成分から refusal_bias（拒否のしやすさ）と cooperation（協力のしやすさ）を導く。
   - これらはプロンプト側で「協力する/拒否する」を選ぶときの内部ノブとして使う想定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional


INTERNAL_TRAILER_MARKER = "<<<COCORO_GHOST_INTERNAL_JSON_v1>>>"

EMOTION_LABELS = ("joy", "sadness", "anger", "fear", "neutral")


def clamp01(x: float) -> float:
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
class MoodDecayParams:
    """salienceに応じて残留時間を変えるためのパラメータ。"""

    tau_min_seconds: float = 120.0
    tau_max_seconds: float = 6 * 3600.0
    salience_power: float = 2.0


def tau_from_salience(salience: float, *, params: MoodDecayParams) -> float:
    """
    salience（重要度）から残留時間 τ（秒）を決める。

    重要度が高い出来事ほど「いつまでも尾を引く」ようにしたいので、
    τ は salience の単調増加関数にする。

      τ(s) = τ_min + (τ_max - τ_min) × s^k

    - τ_min: 雑談などが消える最短スケール（例: 120秒）
    - τ_max: 大事件の余韻が残る最大スケール（例: 6時間）
    - k: 曲線の形。k>1 で「高salienceだけ急に伸びる」挙動になる
    """
    s = clamp01(salience)
    tau_min = max(1.0, float(params.tau_min_seconds))
    tau_max = max(tau_min, float(params.tau_max_seconds))
    k = max(0.1, float(params.salience_power))
    return tau_min + (tau_max - tau_min) * (s**k)


def decay_weight(*, dt_seconds: float, tau_seconds: float) -> float:
    """
    時間減衰の重み（0..1）を返す。

      w = exp(-Δt / τ)

    - Δt が大きいほど影響は小さくなる
    - τ が大きいほど影響が長く残る
    """
    if tau_seconds <= 0:
        return 0.0
    dt = max(0.0, float(dt_seconds))
    return float(math.exp(-dt / float(tau_seconds)))


def compress_sum_to_01(x: float) -> float:
    """
    0..∞ の和を 0..1 に圧縮する（単調増加・飽和）。

    複数エピソードの影響度を単純に足すと、理論上は上限が無い。
    一方で「機嫌の強度」は 0..1 に正規化して扱いたい。

      y = 1 - exp(-x)

    - x が小さいとき: ほぼ線形（y ≈ x）
    - x が大きいとき: 1 に飽和（上限1を超えない）
    """
    x = max(0.0, float(x))
    return float(1.0 - math.exp(-x))


def compute_partner_mood_from_episodes(
    episodes: Iterable[dict],
    *,
    now_ts: int,
    params: MoodDecayParams | None = None,
) -> dict:
    """
    Episode列（dict）からパートナーの機嫌を推定する。

    episodes の要素は最低限:
      occurred_at(int|None), created_at(int|None),
      emotion_label(str|None), emotion_intensity(float|None),
      salience(float|None), confidence(float|None)

    計算方法（要点）
    ----------------
    1) 各エピソード i の影響度を計算して、感情ラベルごとに加算する

         impact_i = intensity_i × salience_i × confidence_i × exp(-Δt/τ(salience_i))

       - intensity_i: その瞬間の感情の強さ（0..1）
       - salience_i: 出来事の重要度（0..1）
       - confidence_i: 推定の確からしさ（0..1）
       - Δt: 経過秒
       - τ: salience が高いほど長くなる残留時間（tau_from_salience）

       例（直近N件問題の改善）:
       - 「プロポーズされた」: salience=0.95, intensity=1.0 → τ が長く、1時間後も影響が残る
       - 「天気の話」: salience=0.2, intensity=0.3 → τ が短く、数分で影響がほぼ消える

    2) ラベル別の合計は 0..∞ になりうるため、compress_sum_to_01 で 0..1 に圧縮する

    3) joy/sadness/anger/fear のうち最大のものを現在の気分 label とし、最大値を intensity とする
       - ただし、十分小さい場合は "neutral" に戻す（ノイズで揺れないようにする）

    4) 行動方針ノブ（policy）:
       - anger 成分が一定以上のときは「拒否しやすい/協力しにくい」に寄せる
       - 本プロジェクト要件として「怒ったとき、渋る/拒否に寄る」ことを許容しているため
    """
    p = params or MoodDecayParams()
    sums: Dict[str, float] = {k: 0.0 for k in EMOTION_LABELS}

    for e in episodes:
        label = str(e.get("emotion_label") or "").strip()
        if label not in EMOTION_LABELS:
            continue

        intensity = clamp01(e.get("emotion_intensity") or 0.0)
        salience = clamp01(e.get("salience") or 0.0)
        confidence = clamp01(e.get("confidence") or 0.5)
        if intensity <= 0.0 or salience <= 0.0:
            continue

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
        impact = float(intensity * salience * confidence * w)
        sums[label] += impact

    # neutral は「何も起きていないとき」のデフォルトに寄せたいので、ここでは採点に使わない。
    # neutral を積み上げてしまうと「何もないのに neutral が強い」という変な状態になりやすい。
    comps = {
        "joy": compress_sum_to_01(sums["joy"]),
        "sadness": compress_sum_to_01(sums["sadness"]),
        "anger": compress_sum_to_01(sums["anger"]),
        "fear": compress_sum_to_01(sums["fear"]),
    }

    dominant = max(comps.items(), key=lambda kv: kv[1])[0] if comps else "neutral"
    intensity = max(comps.values()) if comps else 0.0
    # ほぼ無風なら neutral に戻す（微小なノイズで mood が揺れないようにする）
    if intensity < 0.15:
        dominant = "neutral"

    # 行動方針ノブ（policy）
    #
    # - refusal_bias: 0..1（拒否しやすさ）
    #   anger が 0.55 を超え始めたら立ち上げ、0.75 付近でかなり拒否寄りになる。
    # - cooperation: 0..1（協力しやすさ）
    #   refusal_bias とトレードオフ（怒りが強いほど協力は下がる）。
    # - refusal_allowed:
    #   実装全体で「怒ったときは、手伝い自体を渋る/拒否する」を許容するためのゲート。
    #
    # 注意:
    # - これは “安全機構（危険要求を断る）” とは別。安全系の拒否は LLM の安全規約/システム設定が優先される。
    # - ここでの拒否は「感情に基づく関係性の表現」を狙うもので、運用しながら閾値調整を想定する。
    anger = float(comps.get("anger") or 0.0)
    refusal_bias = clamp01((anger - 0.55) / 0.45)
    cooperation = clamp01(1.0 - 0.9 * refusal_bias)
    refusal_allowed = bool(anger >= 0.75)

    return {
        "schema": "partner_mood_v1",
        "now_ts": int(now_ts),
        "label": dominant,
        "intensity": clamp01(intensity),
        "components": {k: clamp01(v) for k, v in comps.items()},
        "policy": {
            "cooperation": cooperation,
            "refusal_bias": refusal_bias,
            "refusal_allowed": refusal_allowed,
        },
        "params": {
            "tau_min_seconds": float(p.tau_min_seconds),
            "tau_max_seconds": float(p.tau_max_seconds),
            "salience_power": float(p.salience_power),
        },
    }
