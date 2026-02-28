"""
感情（VAD）と LongMoodState（長期気分）の共通ロジック。

目的:
    - worker.py / memory.py に散らばっていた「VAD計算」「半減期」「LongMoodState更新」を集約する。
    - アルゴリズムの一貫性を保ち、調整点（半減期など）を1箇所に寄せる。

前提:
    - VAD は各軸 -1.0..+1.0（v=valence, a=arousal, d=dominance）。
    - LongMoodState は「基調（baseline）」と「余韻（shock）」の2層で表す。
      - baseline: 日スケールでゆっくり変わる
      - shock: 直近の出来事に強く反応し、時間で減衰する（重大イベントほど減衰を遅くする）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cocoro_ghost.core.common_utils import json_loads_maybe as _json_loads_maybe

# --- LongMoodState パラメータ（1箇所で管理） ---
LONG_MOOD_MODEL_VERSION = 3

# NOTE:
# - baseline は「1日くらい」で変化させたいので、半減期=24h を既定とする。
# - shock は「余韻」なので、半減期は可変にする（小さな出来事は短く、重大な出来事は長い）。
LONG_MOOD_BASELINE_HALFLIFE_SECONDS = 24 * 60 * 60
LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN = 60 * 60
LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MAX = 24 * 60 * 60

# NOTE:
# - shock の追従率は dt（イベント間隔）に依存させない。出来事が起きたら即座に効いてほしいため。
LONG_MOOD_SHOCK_ALPHA_BASE = 0.6


@dataclass(frozen=True)
class LongMoodUpdateResult:
    """LongMoodState の更新結果（DB保存のための値）を表す。"""

    body_text: str
    payload_obj: dict[str, Any]
    confidence: float
    last_confirmed_at: int


def clamp_vad(x: Any) -> float:
    """VAD値を -1.0..+1.0 に丸める（壊れた出力でもDBを壊さないため）。"""
    try:
        v = float(x)
    except Exception:  # noqa: BLE001
        return 0.0
    if v < -1.0:
        return -1.0
    if v > 1.0:
        return 1.0
    return v


def clamp_01(x: Any) -> float:
    """0.0..1.0 に丸める（壊れた出力でもDBを壊さないため）。"""
    try:
        v = float(x)
    except Exception:  # noqa: BLE001
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def alpha_from_halflife(*, dt_seconds: int, half_life_seconds: int) -> float:
    """半減期（秒）から、時刻差 dt の EMA 係数 alpha を返す。"""
    dt = int(dt_seconds)
    hl = int(half_life_seconds)
    if dt <= 0:
        return 0.0
    if hl <= 0:
        return 1.0

    # --- 1 - 0.5^(dt/hl) を採用（dt=hl で 0.5 だけ追従） ---
    try:
        a = 1.0 - (0.5 ** (float(dt) / float(hl)))
    except Exception:  # noqa: BLE001
        a = 0.0
    return clamp_01(a)


def decay_from_halflife(*, dt_seconds: int, half_life_seconds: int) -> float:
    """半減期（秒）から、時刻差 dt の減衰係数（0.0..1.0）を返す。"""
    dt = int(dt_seconds)
    hl = int(half_life_seconds)
    if dt <= 0:
        return 1.0
    if hl <= 0:
        return 0.0
    try:
        return clamp_01(0.5 ** (float(dt) / float(hl)))
    except Exception:  # noqa: BLE001
        return 1.0


def vad_dict(v: Any, a: Any, d: Any) -> dict[str, float]:
    """VAD を dict へ正規化して返す。"""
    return {"v": clamp_vad(v), "a": clamp_vad(a), "d": clamp_vad(d)}


def vad_add(x: dict[str, float], y: dict[str, float]) -> dict[str, float]:
    """VAD同士を加算する（各軸はclampする）。"""
    return vad_dict(
        float(x.get("v", 0.0)) + float(y.get("v", 0.0)),
        float(x.get("a", 0.0)) + float(y.get("a", 0.0)),
        float(x.get("d", 0.0)) + float(y.get("d", 0.0)),
    )


def vad_sub(x: dict[str, float], y: dict[str, float]) -> dict[str, float]:
    """VAD同士を減算する（各軸はclampする）。"""
    return vad_dict(
        float(x.get("v", 0.0)) - float(y.get("v", 0.0)),
        float(x.get("a", 0.0)) - float(y.get("a", 0.0)),
        float(x.get("d", 0.0)) - float(y.get("d", 0.0)),
    )


def vad_scale(x: dict[str, float], scale: float) -> dict[str, float]:
    """VADをスカラー倍する（各軸はclampする）。"""
    s = float(scale)
    return vad_dict(float(x.get("v", 0.0)) * s, float(x.get("a", 0.0)) * s, float(x.get("d", 0.0)) * s)


def vad_lerp(cur: dict[str, float], tgt: dict[str, float], alpha: float) -> dict[str, float]:
    """VAD を EMA で更新する（cur + alpha*(tgt-cur)）。"""
    a = clamp_01(alpha)
    dv = vad_sub(tgt, cur)
    return vad_add(cur, {"v": float(dv["v"]) * a, "a": float(dv["a"]) * a, "d": float(dv["d"]) * a})


def local_day_key(ts_utc: int) -> str:
    """UTCのUNIX秒から、ローカル日付キー（YYYY-MM-DD）を返す。"""
    dt = datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).astimezone()
    return dt.date().isoformat()


def sanitize_moment_affect_labels(labels_in: Any) -> list[str]:
    """
    moment_affect_labels を保存用に正規化する。

    方針:
        - list[str] を期待するが、壊れた出力でも落とさない（空にする）
        - 余計な改行を除去し、空要素を捨てる
        - 重複は除去（順序は維持）
        - 上限数を設けて入力肥大化を防ぐ
    """
    if not isinstance(labels_in, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in labels_in:
        label = str(item or "").replace("\n", " ").replace("\r", " ").strip()
        if not label:
            continue
        label = label[:24]
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= 6:
            break
    return out


def parse_long_mood_payload(payload_json: str) -> dict[str, Any]:
    """long_mood_state の payload_json を dict として返す（壊れていても落とさない）。"""
    obj = _json_loads_maybe(payload_json)
    return obj if isinstance(obj, dict) else {}


def extract_vad_from_payload_obj(obj: dict[str, Any], key: str) -> dict[str, float] | None:
    """payload dict から VAD（{"v","a","d"}）を読む（無ければ None）。"""
    v = obj.get(key)
    if not isinstance(v, dict):
        return None
    if not all(k in v for k in ("v", "a", "d")):
        return None
    try:
        return vad_dict(v.get("v"), v.get("a"), v.get("d"))
    except Exception:  # noqa: BLE001
        return None


def build_long_mood_payload(
    *,
    baseline_vad: dict[str, float],
    shock_vad: dict[str, float],
    shock_note: str | None,
    baseline_day_key: str | None,
) -> dict[str, Any]:
    """long_mood_state の payload を標準形へ整形する。"""
    out: dict[str, Any] = {
        "model_version": int(LONG_MOOD_MODEL_VERSION),
        "baseline_vad": vad_dict(baseline_vad.get("v"), baseline_vad.get("a"), baseline_vad.get("d")),
        "shock_vad": vad_dict(shock_vad.get("v"), shock_vad.get("a"), shock_vad.get("d")),
        "baseline_day_key": (str(baseline_day_key) if baseline_day_key else None),
    }
    note = str(shock_note or "").strip()
    if note:
        out["shock_note"] = note[:240]
    return out


def decay_shock_for_snapshot(
    *,
    shock_vad: dict[str, float] | None,
    dt_seconds: int,
    shock_halflife_seconds: int = 0,
) -> dict[str, float]:
    """
    余韻（shock）を読み出し時点で時間減衰させた VAD を返す。

    NOTE:
        - 会話が止まっている間も自然に落ち着くように、読み出し側で減衰させる。
        - dt は now_ts - last_confirmed_at を想定する。
    """
    base = shock_vad if shock_vad is not None else vad_dict(0.0, 0.0, 0.0)
    hl = int(shock_halflife_seconds or 0)
    if hl <= 0:
        hl = int(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN)
    decay = decay_from_halflife(dt_seconds=int(dt_seconds), half_life_seconds=int(hl))
    return vad_scale(base, float(decay))


def shock_severity(*, baseline_vad: dict[str, float], moment_vad: dict[str, float]) -> float:
    """
    baseline と moment の乖離から、ショックの強さ（0.0..1.0）を返す。

    方針:
        - 各軸は -1..+1 なので、差の最大は 2
        - ここでは「最大軸差 / 2」を severity とする（直感的で調整が容易）
    """
    dv = vad_sub(moment_vad, baseline_vad)
    major_delta = max(abs(float(dv["v"])), abs(float(dv["a"])), abs(float(dv["d"])))
    return clamp_01(float(major_delta) / 2.0)


def shock_halflife_seconds_from_severity(sev: float) -> int:
    """
    severity から shock の半減期（秒）を返す。

    方針:
        - 小さな出来事はすぐ落ち着く（min）
        - 重大な出来事は長く尾を引く（max）
        - 曲線は二乗で「軽い出来事を短く」寄りにする
    """
    s = clamp_01(sev)
    w = float(s) * float(s)
    hl = int(round(float(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN) + (float(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MAX) - float(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN)) * w))
    return max(int(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN), min(int(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MAX), int(hl)))


def shock_alpha_from_severity(sev: float) -> float:
    """
    severity から shock の追従率（0.0..1.0）を返す。

    NOTE:
        - 重大イベントは「即座に効いてほしい」ため、alpha をやや上げる。
        - ただし 1.0 に固定するとノイズを増やしやすいので、上限は控えめにする。
    """
    s = clamp_01(sev)
    return clamp_01(float(LONG_MOOD_SHOCK_ALPHA_BASE) + 0.3 * float(s))


def update_long_mood(
    *,
    prev_state_exists: bool,
    prev_payload_obj: dict[str, Any] | None,
    prev_body_text: str | None,
    prev_confidence: float | None,
    prev_last_confirmed_at: int | None,
    event_ts: int,
    moment_vad: dict[str, float] | None,
    moment_confidence: float,
    moment_note: str | None,
    baseline_text_candidate: str | None,
) -> LongMoodUpdateResult:
    """
    LongMoodState（baseline + shock）を更新し、DB保存用の値を返す。

    更新ルール:
        - event_affect がある場合（moment_vad がある）:
            - baseline: 半減期=24h の EMA で更新（confidence で重み付け）
            - shock: 直前shockを時間減衰→（moment - baseline_new）へ追従（重大イベントほど減衰を遅くする）
            - last_confirmed_at: event_ts へ更新
        - event_affect が無い場合:
            - 数値は更新しない（baseline/shock/last_confirmed_at を維持）
            - ただし、日付が変わり、本文案があれば body_text だけ更新できる
    """
    ts = int(event_ts)

    # --- 既存値（初回は既定値で開始） ---
    payload_prev = prev_payload_obj if isinstance(prev_payload_obj, dict) else {}
    baseline_prev = extract_vad_from_payload_obj(payload_prev, "baseline_vad")
    shock_prev = extract_vad_from_payload_obj(payload_prev, "shock_vad")

    # --- baseline/shock の初期値 ---
    baseline_vad = baseline_prev if baseline_prev is not None else (dict(moment_vad) if moment_vad is not None else vad_dict(0.0, 0.0, 0.0))
    shock_vad = shock_prev if shock_prev is not None else vad_dict(0.0, 0.0, 0.0)

    # --- shock 半減期（前回値。無ければ最小） ---
    shock_halflife_prev = int(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN)
    if isinstance(payload_prev, dict):
        try:
            shock_halflife_prev = int(payload_prev.get("shock_halflife_seconds") or 0)
        except Exception:  # noqa: BLE001
            shock_halflife_prev = int(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN)
    if shock_halflife_prev <= 0:
        shock_halflife_prev = int(LONG_MOOD_SHOCK_HALFLIFE_SECONDS_MIN)

    # --- baseline_day_key の初期値 ---
    baseline_day_key = str(payload_prev.get("baseline_day_key") or "").strip()
    if not baseline_day_key:
        baseline_day_key = local_day_key(int(ts))

    # --- baseline_text の初期値（既存があれば安定性優先で保持） ---
    baseline_text = str(prev_body_text or "").strip() if prev_state_exists else ""
    if not baseline_text:
        baseline_text = str(baseline_text_candidate or "").strip()

    # --- confidence/shock_note は「欠落しない」を正にする ---
    confidence_for_save = float(prev_confidence) if (prev_state_exists and prev_confidence is not None) else 0.0
    shock_note_for_save: str | None = None
    if str(payload_prev.get("shock_note") or "").strip():
        shock_note_for_save = str(payload_prev.get("shock_note") or "").strip()

    # --- event_affect がある場合は今回値を使う ---
    moment_conf = clamp_01(moment_confidence)
    has_moment = moment_vad is not None and float(moment_conf) > 0.0
    if has_moment:
        confidence_for_save = float(moment_conf)
        if str(moment_note or "").strip():
            shock_note_for_save = str(moment_note or "").strip()[:240]

    # --- baseline/shock の更新（event_affect がある場合だけ） ---
    baseline_vad_new = dict(baseline_vad)
    shock_vad_new = dict(shock_vad)
    last_confirmed_at_new = int(prev_last_confirmed_at or 0)
    if not prev_state_exists:
        last_confirmed_at_new = int(ts)

    if has_moment:
        # --- dt を計算（last_confirmed_at は「数値更新の時刻」を意味する） ---
        try:
            dt_seconds = int(ts) - int(prev_last_confirmed_at or 0)
        except Exception:  # noqa: BLE001
            dt_seconds = 0
        if dt_seconds < 0:
            dt_seconds = 0

        # --- baseline を日スケールで更新（confidence で重み付け） ---
        alpha_base = alpha_from_halflife(
            dt_seconds=int(dt_seconds), half_life_seconds=int(LONG_MOOD_BASELINE_HALFLIFE_SECONDS)
        )
        alpha_base = clamp_01(float(alpha_base) * float(moment_conf))

        baseline_vad_new = vad_lerp(baseline_vad, moment_vad, float(alpha_base))

        # --- shock は短期で追従し、時間で減衰する ---
        # NOTE:
        # - 重大イベントは余韻が長く残るため、半減期を延ばす。
        # - 余韻の強さ（severity）は baseline_new と moment の乖離で決める。
        sev = shock_severity(baseline_vad=baseline_vad_new, moment_vad=moment_vad)
        shock_halflife_new = shock_halflife_seconds_from_severity(float(sev))

        decay = decay_from_halflife(
            dt_seconds=int(dt_seconds), half_life_seconds=int(shock_halflife_prev)
        )
        shock_decayed = vad_scale(shock_vad, float(decay))

        # --- baselineとの差分へ寄せる（差分は範囲内へclampする） ---
        delta = vad_sub(moment_vad, baseline_vad_new)
        alpha_shock = clamp_01(float(shock_alpha_from_severity(float(sev))) * float(moment_conf))
        shock_vad_new = vad_lerp(shock_decayed, delta, float(alpha_shock))

        # --- 数値更新をしたときだけ last_confirmed_at を進める ---
        last_confirmed_at_new = int(ts)

    # --- body_text の更新は「日付が変わった + 本文案がある」場合だけ（ノイズ防止） ---
    candidate = str(baseline_text_candidate or "").strip()
    if not baseline_text:
        # NOTE: 初回は本文が必須なので、本文案が無ければ最低限で埋める。
        baseline_text = (candidate or str(moment_note or "").strip() or "今日は落ち着かない気分が続いている。")[:600]
        baseline_day_key = local_day_key(int(ts))
    else:
        new_day_key = local_day_key(int(ts))
        # --- 既定: 日付が変わったときだけ差し替える（短期揺れを抑える） ---
        if new_day_key != str(baseline_day_key or "") and candidate:
            baseline_text = candidate[:600]
            baseline_day_key = new_day_key
        # --- 例外: 本文が短すぎる場合は、同日でも本文案で底上げする ---
        # NOTE:
        # - long_mood_state は返答生成へ毎ターン注入されるため、短すぎる本文はトーンへの寄与が弱くなりやすい。
        # - 一方で、毎ターン差し替えると「背景」が短期イベントに引っ張られやすい。
        # - ここでは「最低限の分量」を満たすための救済として、短い場合だけ同日更新を許可する。
        elif candidate and len(str(baseline_text)) < 80:
            baseline_text = candidate[:600]
            baseline_day_key = new_day_key

    # --- payload を標準形へ整形する ---
    payload_new = build_long_mood_payload(
        baseline_vad=baseline_vad_new,
        shock_vad=shock_vad_new,
        shock_note=shock_note_for_save,
        baseline_day_key=baseline_day_key,
    )
    if has_moment:
        # --- 直近イベント由来のパラメータを残す（デバッグ/可視化用） ---
        payload_new["baseline_halflife_seconds"] = int(LONG_MOOD_BASELINE_HALFLIFE_SECONDS)
        payload_new["shock_halflife_seconds"] = int(shock_halflife_new)
        payload_new["shock_severity"] = float(sev)

    return LongMoodUpdateResult(
        body_text=str(baseline_text),
        payload_obj=payload_new,
        confidence=float(confidence_for_save),
        last_confirmed_at=int(last_confirmed_at_new),
    )
