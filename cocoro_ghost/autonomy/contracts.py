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


@dataclass(frozen=True)
class ParsedActionDecision:
    """
    Deliberation 出力（ActionDecision）を正規化した値オブジェクト。
    """

    decision_outcome: str
    do_action: int
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

    # --- do_action を正規化 ---
    do_action_raw = value.get("do_action")
    do_action = 1 if bool(do_action_raw) else 0
    if outcome == "do_action" and do_action != 1:
        raise ValueError("decision_outcome=do_action requires do_action=true")
    if outcome != "do_action" and do_action != 0:
        raise ValueError("decision_outcome!=do_action requires do_action=false")

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

    # --- persona/mood 監査情報 ---
    persona_influence_json = common_utils.json_dumps(value.get("persona_influence") or {})
    mood_influence_json = common_utils.json_dumps(value.get("mood_influence") or {})

    # --- evidence を配列化 ---
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    evidence_event_ids = evidence.get("event_ids") if isinstance(evidence.get("event_ids"), list) else []
    evidence_state_ids = evidence.get("state_ids") if isinstance(evidence.get("state_ids"), list) else []
    evidence_goal_ids = evidence.get("goal_ids") if isinstance(evidence.get("goal_ids"), list) else []

    return ParsedActionDecision(
        decision_outcome=str(outcome),
        do_action=int(do_action),
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
