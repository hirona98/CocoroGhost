"""
current_thought_state の構造化ヘルパ。

役割:
- past / present / future 形式の正規化
- 旧 payload 形式との互換吸収
- 自発判断で使う抽出関数の共通化
"""

from __future__ import annotations

from typing import Any


# --- 行動を再点火しやすい target 種別は、待機中の present からは切り離せるようにしておく ---
_ACTIONABLE_TARGET_TYPES = {
    "action_type",
    "capability",
    "world_capability",
    "world_goal",
    "world_query",
}


def normalize_current_thought_attention_targets(
    raw: Any,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """attention_targets を安全な構造へ正規化する。"""

    # --- 配列以外は空として扱う ---
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for item in list(raw):
        if not isinstance(item, dict):
            continue
        target_type = str(item.get("type") or "").strip()
        value = str(item.get("value") or "").strip()
        if not target_type or not value:
            continue
        updated_at = item.get("updated_at")
        out.append(
            {
                "type": str(target_type),
                "value": str(value),
                "weight": float(item.get("weight") or 0.0),
                "updated_at": (
                    int(updated_at)
                    if isinstance(updated_at, (int, float)) and int(updated_at) > 0
                    else None
                ),
            }
        )
        if len(out) >= int(limit):
            break
    return out


def normalize_current_thought_next_action(raw: Any) -> dict[str, Any] | None:
    """next_action / next_candidate_action を互換込みで正規化する。"""

    # --- 辞書以外は未設定として扱う ---
    if not isinstance(raw, dict):
        return None

    action_type = str(raw.get("action_type") or "").strip()
    if not action_type:
        return None

    # --- 新旧キー（action_payload / payload）の両方を受ける ---
    action_payload = raw.get("action_payload")
    if not isinstance(action_payload, dict):
        action_payload = raw.get("payload")
    if not isinstance(action_payload, dict):
        action_payload = {}

    payload_obj = dict(action_payload)
    return {
        "action_type": str(action_type),
        "action_payload": dict(payload_obj),
        "payload": dict(payload_obj),
    }


def normalize_current_thought_resume_when(raw: Any) -> dict[str, Any] | None:
    """future.resume_when を安全な構造へ正規化する。"""

    # --- 辞書以外は未設定として扱う ---
    if not isinstance(raw, dict):
        return None

    mode = str(raw.get("mode") or "").strip()
    if not mode:
        return None

    not_before_ts_raw = raw.get("not_before_ts")
    not_before_ts = (
        int(not_before_ts_raw)
        if isinstance(not_before_ts_raw, (int, float)) and int(not_before_ts_raw) > 0
        else None
    )

    return {
        "mode": str(mode),
        "requires_user_reply": bool(raw.get("requires_user_reply")),
        "requires_new_observation": bool(raw.get("requires_new_observation")),
        "not_before_ts": not_before_ts,
    }


def normalize_current_thought_snapshot(
    *,
    state_id: int,
    body_text: str,
    payload_obj: Any,
    confidence: float,
    last_confirmed_at: int,
) -> dict[str, Any]:
    """current_thought_state payload を新旧互換込みで正規化する。"""

    # --- payload は object 前提だが、壊れていても空で進める ---
    if not isinstance(payload_obj, dict):
        payload_obj = {}

    past_raw = payload_obj.get("past") if isinstance(payload_obj.get("past"), dict) else {}
    present_raw = payload_obj.get("present") if isinstance(payload_obj.get("present"), dict) else {}
    future_raw = payload_obj.get("future") if isinstance(payload_obj.get("future"), dict) else {}

    # --- attention_targets は新形式 present を優先し、無ければ旧トップレベルへ戻す ---
    attention_targets = normalize_current_thought_attention_targets(
        (
            present_raw.get("attention_targets")
            if isinstance(present_raw.get("attention_targets"), list)
            else payload_obj.get("attention_targets")
        )
    )

    # --- 現在の関わり方は present 側を正本にする ---
    interaction_mode = (
        str(present_raw.get("interaction_mode") or "").strip()
        or str(payload_obj.get("interaction_mode") or "").strip()
        or None
    )

    # --- 現在フォーカスは present.focus を正本にし、旧フィールドも読む ---
    focus_raw = present_raw.get("focus") if isinstance(present_raw.get("focus"), dict) else {}
    active_thread_id = (
        str(focus_raw.get("thread_id") or "").strip()
        or str(payload_obj.get("active_thread_id") or "").strip()
        or None
    )
    focus_summary = (
        str(focus_raw.get("summary") or "").strip()
        or str(payload_obj.get("focus_summary") or "").strip()
        or None
    )
    focus_topic = str(focus_raw.get("topic") or "").strip() or None

    # --- 次の行動は future.next_action を正本にする ---
    next_action = normalize_current_thought_next_action(future_raw.get("next_action"))
    if next_action is None:
        next_action = normalize_current_thought_next_action(payload_obj.get("next_candidate_action"))

    # --- 再開条件は future.resume_when をそのまま読む ---
    resume_when = normalize_current_thought_resume_when(future_raw.get("resume_when"))

    # --- 更新元イベントIDは監査用に残す ---
    updated_from_event_ids_raw = payload_obj.get("updated_from_event_ids")
    updated_from_event_ids = []
    if isinstance(updated_from_event_ids_raw, list):
        for x in list(updated_from_event_ids_raw):
            if isinstance(x, (int, float)) and int(x) > 0:
                updated_from_event_ids.append(int(x))
    updated_from_event_ids = updated_from_event_ids[-20:]

    # --- present.appraisal は新形式のみ。無ければ空で返す ---
    appraisal_raw = present_raw.get("appraisal") if isinstance(present_raw.get("appraisal"), dict) else {}
    present_appraisal = {
        "novelty": str(appraisal_raw.get("novelty") or "").strip() or None,
        "status": str(appraisal_raw.get("status") or "").strip() or None,
        "reason": str(appraisal_raw.get("reason") or "").strip() or None,
    }

    # --- 過去の要約は文字列だけに絞る ---
    last_event_raw = past_raw.get("last_event") if isinstance(past_raw.get("last_event"), dict) else {}
    past_last_event = {
        "event_id": (
            int(last_event_raw.get("event_id"))
            if isinstance(last_event_raw.get("event_id"), (int, float)) and int(last_event_raw.get("event_id")) > 0
            else None
        ),
        "event_source": str(last_event_raw.get("event_source") or "").strip() or None,
        "at": (
            int(last_event_raw.get("at"))
            if isinstance(last_event_raw.get("at"), (int, float)) and int(last_event_raw.get("at")) > 0
            else None
        ),
    }

    # --- 返却は新形式を正本にしつつ、既存コード向けの互換キーも残す ---
    schema_version_raw = payload_obj.get("schema_version")
    schema_version = int(schema_version_raw) if isinstance(schema_version_raw, (int, float)) and int(schema_version_raw) > 0 else 1

    return {
        "state_id": int(state_id),
        "kind": "current_thought_state",
        "body_text": str(body_text or ""),
        "interaction_mode": (str(interaction_mode) if interaction_mode else None),
        "active_thread_id": (str(active_thread_id) if active_thread_id else None),
        "focus_summary": (str(focus_summary) if focus_summary else None),
        "next_candidate_action": (dict(next_action) if isinstance(next_action, dict) else None),
        "attention_targets": list(attention_targets),
        "updated_from_event_ids": [int(x) for x in list(updated_from_event_ids)],
        "updated_from": (payload_obj.get("updated_from") if isinstance(payload_obj.get("updated_from"), dict) else None),
        "updated_reason": str(payload_obj.get("updated_reason") or "").strip() or None,
        "confidence": float(confidence),
        "last_confirmed_at": int(last_confirmed_at),
        "schema_version": int(schema_version),
        "past": {
            "continuity_summary": str(past_raw.get("continuity_summary") or "").strip() or None,
            "last_event": dict(past_last_event),
            "recent_event_ids": [
                int(x)
                for x in list(past_raw.get("recent_event_ids") or [])
                if isinstance(x, (int, float)) and int(x) > 0
            ][-20:],
            "last_completed_action": (
                dict(past_raw.get("last_completed_action"))
                if isinstance(past_raw.get("last_completed_action"), dict)
                else None
            ),
        },
        "present": {
            "interaction_mode": (str(interaction_mode) if interaction_mode else None),
            "focus": {
                "summary": (str(focus_summary) if focus_summary else None),
                "thread_id": (str(active_thread_id) if active_thread_id else None),
                "topic": (str(focus_topic) if focus_topic else None),
            },
            "appraisal": dict(present_appraisal),
            "attention_targets": list(attention_targets),
        },
        "future": {
            "next_action": (dict(next_action) if isinstance(next_action, dict) else None),
            "resume_when": (dict(resume_when) if isinstance(resume_when, dict) else None),
            "desire_summary": str(future_raw.get("desire_summary") or "").strip() or None,
        },
    }


def extract_current_thought_attention_targets(
    snapshot: dict[str, Any] | None,
    *,
    include_actionable: bool,
) -> list[dict[str, Any]]:
    """正規化済み current_thought から、必要な target だけを返す。"""

    # --- 無効入力は空として扱う ---
    if not isinstance(snapshot, dict):
        return []

    present_raw = snapshot.get("present") if isinstance(snapshot.get("present"), dict) else {}
    raw_targets = (
        present_raw.get("attention_targets")
        if isinstance(present_raw.get("attention_targets"), list)
        else snapshot.get("attention_targets")
    )
    targets = normalize_current_thought_attention_targets(raw_targets)
    if include_actionable:
        return list(targets)
    return [
        dict(item)
        for item in list(targets)
        if str(item.get("type") or "").strip() not in _ACTIONABLE_TARGET_TYPES
    ]


def extract_current_thought_interaction_mode(snapshot: dict[str, Any] | None) -> str | None:
    """正規化済み current_thought から interaction_mode を返す。"""

    # --- 新形式 present を優先する ---
    if not isinstance(snapshot, dict):
        return None
    present_raw = snapshot.get("present") if isinstance(snapshot.get("present"), dict) else {}
    value = str(present_raw.get("interaction_mode") or "").strip() or str(snapshot.get("interaction_mode") or "").strip()
    return str(value) if value else None


def extract_current_thought_focus_summary(snapshot: dict[str, Any] | None) -> str | None:
    """正規化済み current_thought からフォーカス要約を返す。"""

    # --- 新形式 present.focus を優先する ---
    if not isinstance(snapshot, dict):
        return None
    present_raw = snapshot.get("present") if isinstance(snapshot.get("present"), dict) else {}
    focus_raw = present_raw.get("focus") if isinstance(present_raw.get("focus"), dict) else {}
    value = str(focus_raw.get("summary") or "").strip() or str(snapshot.get("focus_summary") or "").strip()
    return str(value) if value else None


def extract_current_thought_next_action(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """正規化済み current_thought から次の行動候補を返す。"""

    # --- 新形式 future を優先し、無ければ互換キーへ戻す ---
    if not isinstance(snapshot, dict):
        return None
    future_raw = snapshot.get("future") if isinstance(snapshot.get("future"), dict) else {}
    next_action = normalize_current_thought_next_action(future_raw.get("next_action"))
    if next_action is not None:
        return dict(next_action)
    return normalize_current_thought_next_action(snapshot.get("next_candidate_action"))


def extract_current_thought_resume_when(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """正規化済み current_thought から future.resume_when を返す。"""

    # --- future が無ければ未設定として扱う ---
    if not isinstance(snapshot, dict):
        return None
    future_raw = snapshot.get("future") if isinstance(snapshot.get("future"), dict) else {}
    return normalize_current_thought_resume_when(future_raw.get("resume_when"))
