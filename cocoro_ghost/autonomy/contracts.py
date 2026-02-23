"""
autonomy 契約モデル。

目的:
    - Deliberation/Execution の入出力契約を1箇所に固定する。
    - Worker実装側で同じ検証ロジックを再利用する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cocoro_ghost import common_utils


_DECISION_OUTCOMES = {"do_action", "skip", "defer"}
_RESULT_STATUSES = {"success", "partial", "failed", "no_effect"}
_PERSONA_PREFERRED_DIRECTIONS = {"observe", "support", "wait", "avoid", "explore"}
_THRESHOLD_BIASES = {"higher", "neutral", "lower"}


def _normalize_string_list(value: Any, *, field_name: str, min_items: int = 0) -> list[str]:
    """
    list[str] を正規化して返す。

    Deliberation の JSON 契約では、人格/気分の影響説明に文字列配列を使うため、
    ここで最小限の型検証を行う。
    """
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        out.append(text)
    if len(out) < int(min_items):
        raise ValueError(f"{field_name} must contain at least {int(min_items)} item(s)")
    return out


def _normalize_enum_text(
    value: Any,
    *,
    field_name: str,
    allowed: set[str],
) -> str:
    """
    列挙値（文字列）を正規化して返す。

    方針:
        - 自然文の意味判定は行わない。
        - 事前定義した構造列挙だけを許可する。
    """
    text = str(value or "").strip()
    if text not in allowed:
        allowed_text = "/".join(sorted(str(x) for x in allowed))
        raise ValueError(f"{field_name} must be one of {allowed_text}")
    return str(text)


def _parse_persona_influence(value: Any) -> dict[str, Any]:
    """
    persona_influence を検証して正規化する。

    方針:
        - 文字列比較による意味判定はしない。
        - 構造（summary/traits）が埋まっていることだけを保証する。
    """
    if not isinstance(value, dict):
        raise ValueError("persona_influence must be an object")
    summary = str(value.get("summary") or "").strip()
    if not summary:
        raise ValueError("persona_influence.summary is required")
    traits = _normalize_string_list(value.get("traits"), field_name="persona_influence.traits", min_items=1)
    preferred_direction = _normalize_enum_text(
        value.get("preferred_direction"),
        field_name="persona_influence.preferred_direction",
        allowed=_PERSONA_PREFERRED_DIRECTIONS,
    )
    concerns = _normalize_string_list(value.get("concerns"), field_name="persona_influence.concerns", min_items=0)

    out = dict(value)
    out["summary"] = str(summary)
    out["traits"] = list(traits)
    out["preferred_direction"] = str(preferred_direction)
    out["concerns"] = list(concerns)
    return out


def _parse_mood_influence(value: Any) -> dict[str, Any]:
    """
    mood_influence を検証して正規化する。

    方針:
        - mood の影響を説明する summary と VAD 数値の両方を必須にする。
        - 意味の正しさではなく、構造の妥当性を検証する。
    """
    if not isinstance(value, dict):
        raise ValueError("mood_influence must be an object")
    summary = str(value.get("summary") or "").strip()
    if not summary:
        raise ValueError("mood_influence.summary is required")

    vad = value.get("vad")
    if not isinstance(vad, dict):
        raise ValueError("mood_influence.vad must be an object")
    try:
        v = float(vad.get("v"))
        a = float(vad.get("a"))
        d = float(vad.get("d"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("mood_influence.vad must contain numeric v/a/d") from exc

    out = dict(value)
    out["summary"] = str(summary)
    out["vad"] = {"v": float(v), "a": float(a), "d": float(d)}
    out["action_threshold_bias"] = _normalize_enum_text(
        value.get("action_threshold_bias"),
        field_name="mood_influence.action_threshold_bias",
        allowed=_THRESHOLD_BIASES,
    )
    out["defer_bias"] = _normalize_enum_text(
        value.get("defer_bias"),
        field_name="mood_influence.defer_bias",
        allowed=_THRESHOLD_BIASES,
    )
    return out


@dataclass(frozen=True)
class ParsedActionDecision:
    """
    Deliberation 出力（ActionDecision）を正規化した値オブジェクト。
    """

    decision_outcome: str
    action_type: str | None
    action_payload_json: str | None
    priority: int
    reason_text: str
    defer_reason: str | None
    defer_until: int | None
    next_deliberation_at: int | None
    persona_influence_json: str
    mood_influence_json: str
    evidence_event_ids_json: str
    evidence_state_ids_json: str
    evidence_goal_ids_json: str
    confidence: float


@dataclass(frozen=True)
class CapabilityExecutionResult:
    """
    Capability 実行結果の正規化モデル。
    """

    result_status: str
    summary: str
    result_payload_json: str
    useful_for_recall_hint: int
    next_trigger: dict[str, Any] | None


def parse_action_decision(value: dict[str, Any]) -> ParsedActionDecision:
    """
    Deliberation JSON を検証して ParsedActionDecision へ変換する。
    """

    # --- decision_outcome を検証 ---
    outcome = str(value.get("decision_outcome") or "").strip()
    if outcome not in _DECISION_OUTCOMES:
        raise ValueError("decision_outcome must be one of do_action/skip/defer")

    # --- action_type / action_payload を正規化 ---
    action_type = str(value.get("action_type") or "").strip() or None
    action_payload = value.get("action_payload")
    action_payload_json: str | None = None
    if action_payload is not None:
        action_payload_json = common_utils.json_dumps(action_payload)
    elif outcome == "do_action":
        # do_action では payload 必須。空オブジェクトは許容する。
        action_payload_json = "{}"

    if outcome == "do_action":
        if not action_type:
            raise ValueError("decision_outcome=do_action requires action_type")
        if action_payload_json is None or not str(action_payload_json).strip():
            raise ValueError("decision_outcome=do_action requires action_payload")

    # --- defer 契約を検証 ---
    defer_reason_raw = value.get("defer_reason")
    defer_reason = str(defer_reason_raw).strip() if defer_reason_raw is not None else None
    defer_until_raw = value.get("defer_until")
    next_deliberation_raw = value.get("next_deliberation_at")
    defer_until = int(defer_until_raw) if defer_until_raw is not None else None
    next_deliberation_at = int(next_deliberation_raw) if next_deliberation_raw is not None else None
    if outcome == "defer":
        if not defer_reason:
            raise ValueError("decision_outcome=defer requires defer_reason")
        if defer_until is None:
            raise ValueError("decision_outcome=defer requires defer_until")
        if next_deliberation_at is None:
            raise ValueError("decision_outcome=defer requires next_deliberation_at")
        if int(next_deliberation_at) < int(defer_until):
            raise ValueError("next_deliberation_at must be >= defer_until")

    # --- priority / reason / confidence ---
    priority = int(value.get("priority") or 50)
    priority = max(0, min(100, priority))
    reason_text = str(value.get("reason") or "").strip()
    if not reason_text:
        reason_text = "no_reason"
    confidence = float(value.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))

    # --- persona/mood 監査情報（判断で使った構造を必須化） ---
    persona_influence_obj = _parse_persona_influence(value.get("persona_influence"))
    mood_influence_obj = _parse_mood_influence(value.get("mood_influence"))
    persona_influence_json = common_utils.json_dumps(persona_influence_obj)
    mood_influence_json = common_utils.json_dumps(mood_influence_obj)

    # --- evidence を配列化 ---
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    evidence_event_ids = evidence.get("event_ids") if isinstance(evidence.get("event_ids"), list) else []
    evidence_state_ids = evidence.get("state_ids") if isinstance(evidence.get("state_ids"), list) else []
    evidence_goal_ids = evidence.get("goal_ids") if isinstance(evidence.get("goal_ids"), list) else []

    return ParsedActionDecision(
        decision_outcome=str(outcome),
        action_type=action_type,
        action_payload_json=action_payload_json,
        priority=int(priority),
        reason_text=str(reason_text),
        defer_reason=(str(defer_reason) if defer_reason else None),
        defer_until=(int(defer_until) if defer_until is not None else None),
        next_deliberation_at=(int(next_deliberation_at) if next_deliberation_at is not None else None),
        persona_influence_json=str(persona_influence_json),
        mood_influence_json=str(mood_influence_json),
        evidence_event_ids_json=common_utils.json_dumps(evidence_event_ids),
        evidence_state_ids_json=common_utils.json_dumps(evidence_state_ids),
        evidence_goal_ids_json=common_utils.json_dumps(evidence_goal_ids),
        confidence=float(confidence),
    )


def parse_capability_result(value: dict[str, Any]) -> CapabilityExecutionResult:
    """
    Capability 出力 JSON を検証して CapabilityExecutionResult へ変換する。
    """

    # --- result_status を検証 ---
    result_status = str(value.get("result_status") or "").strip()
    if result_status not in _RESULT_STATUSES:
        raise ValueError("result_status must be success/partial/failed/no_effect")

    # --- summary を正規化 ---
    summary = str(value.get("summary") or "").strip()
    if not summary:
        summary = "no_summary"

    # --- payload と recall hint を正規化 ---
    payload = value.get("result_payload")
    result_payload_json = common_utils.json_dumps(payload if payload is not None else {})
    useful_for_recall_hint = 1 if bool(value.get("useful_for_recall_hint")) else 0

    # --- 次トリガがあれば保持 ---
    next_trigger = value.get("next_trigger") if isinstance(value.get("next_trigger"), dict) else None

    return CapabilityExecutionResult(
        result_status=str(result_status),
        summary=str(summary),
        result_payload_json=str(result_payload_json),
        useful_for_recall_hint=int(useful_for_recall_hint),
        next_trigger=next_trigger,
    )
