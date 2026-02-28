"""
Workerジョブハンドラ（autonomy）。

役割:
    - deliberate_once: Trigger から ActionDecision/Intent を生成する。
    - execute_intent: Capability を実行して ActionResult を保存する。
    - promote_action_result_to_searchable: recall可否を最終確定する。
    - snapshot_runtime: runtime snapshot を保存する。
    - sweep_agent_jobs: stale な agent_jobs を timeout 終端する。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils, event_stream, prompt_builders
from cocoro_ghost.autonomy.capabilities.device_control import execute_device_control
from cocoro_ghost.autonomy.capabilities.mobility_move import execute_mobility_move
from cocoro_ghost.autonomy.capabilities.schedule_alarm import execute_schedule_alarm
from cocoro_ghost.autonomy.capabilities.vision_perception import execute_vision_perception
from cocoro_ghost.autonomy.capabilities.web_access import execute_web_research
from cocoro_ghost.autonomy.contracts import (
    CapabilityExecutionResult,
    derive_report_candidate_for_action_result,
    parse_action_decision,
    parse_console_delivery,
    resolve_delivery_mode_from_report_candidate_level,
    resolve_message_kind_for_action_result,
)
from cocoro_ghost.autonomy.runtime_blackboard import get_runtime_blackboard
from cocoro_ghost.clock import get_clock_service
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import (
    AgendaThread,
    ActionDecision,
    ActionResult,
    AgentJob,
    AutonomyTrigger,
    Event,
    EventAffect,
    Intent,
    Job,
    Goal,
    RuntimeSnapshot,
    State,
    UserPreference,
)
from cocoro_ghost.worker_constants import AGENT_JOB_STALE_SECONDS as _AGENT_JOB_STALE_SECONDS
from cocoro_ghost.worker_constants import JOB_PENDING as _JOB_PENDING
from cocoro_ghost.worker_constants import JOB_RUNNING as _JOB_RUNNING
from cocoro_ghost.worker_handlers_common import _now_utc_ts


logger = logging.getLogger(__name__)


class _DeliberationInvalidOutputError(RuntimeError):
    """Deliberation の LLM 出力不正（JSON/契約違反）を表す内部例外。"""

    # --- 種別コードで trigger dropped の理由を安定化する ---
    def __init__(self, *, drop_reason: str, detail: str) -> None:
        super().__init__(str(detail))
        self.drop_reason = str(drop_reason)
        self.detail = str(detail)


def _now_domain_utc_ts() -> int:
    """domain時刻（UTC UNIX秒）を返す。"""
    return int(get_clock_service().now_domain_utc_ts())


def _enqueue_standard_post_event_jobs(
    *,
    db,
    event_id: int,
    now_system_ts: int,
) -> None:
    """
    assistant発話イベント保存後の共通workerジョブを投入する。

    方針:
        - `autonomy.message` は AI人格の発話として events に残す。
        - 後続会話の整合性を保つため、埋め込み/要約/WritePlan を通常イベントと同様に投入する。
    """

    # --- 埋め込み更新 ---
    db.add(
        Job(
            kind="upsert_event_embedding",
            payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
            status=int(_JOB_PENDING),
            run_after=int(now_system_ts),
            tries=0,
            last_error=None,
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )

    # --- assistant本文要約 ---
    db.add(
        Job(
            kind="upsert_event_assistant_summary",
            payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
            status=int(_JOB_PENDING),
            run_after=int(now_system_ts),
            tries=0,
            last_error=None,
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )

    # --- 記憶更新（WritePlan） ---
    db.add(
        Job(
            kind="generate_write_plan",
            payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
            status=int(_JOB_PENDING),
            run_after=int(now_system_ts),
            tries=0,
            last_error=None,
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )


def _publish_autonomy_activity(
    *,
    event_id: int,
    phase: str,
    state: str,
    action_type: str | None,
    capability: str | None,
    backend: str | None,
    result_status: str | None,
    summary_text: str | None,
    decision_id: str | None,
    intent_id: str | None,
    result_id: str | None,
    agent_job_id: str | None,
    goal_id: str | None,
) -> None:
    """
    自発行動の監視用イベント（autonomy.activity）を Console 向けに配信する。

    注意:
        - これは監視用であり、会話履歴の正本にはしない。
        - `event_id=0` は DB保存を伴わない状態変化（agent_job claim/heartbeat 等）で使う。
    """

    # --- 監視UI向けの構造化payloadを作る ---
    payload = {
        "phase": str(phase or ""),
        "state": str(state or ""),
        "action_type": (str(action_type) if action_type else None),
        "capability": (str(capability) if capability else None),
        "backend": (str(backend) if backend else None),
        "result_status": (str(result_status) if result_status else None),
        "summary_text": (str(summary_text) if summary_text else None),
        "decision_id": (str(decision_id) if decision_id else None),
        "intent_id": (str(intent_id) if intent_id else None),
        "result_id": (str(result_id) if result_id else None),
        "agent_job_id": (str(agent_job_id) if agent_job_id else None),
        "goal_id": (str(goal_id) if goal_id else None),
    }
    event_stream.publish(
        type="autonomy.activity",
        event_id=int(event_id),
        data=payload,
        target_client_id=None,
    )


def _resolve_agenda_thread_id_for_decision(
    *,
    db,
    decision: ActionDecision,
) -> str | None:
    """
    ActionDecision から、対象 agenda_thread_id を解決する。

    方針:
        - 新規実装では action_decisions.agenda_thread_id を正本にする。
        - 旧データだけ trigger payload の `agenda_thread_id` を後方互換で読む。
        - action_payload や summary_text からの意味推定はしない。
    """
    # --- 新列が埋まっていれば、それを最優先で使う ---
    agenda_thread_id_from_decision = str(decision.agenda_thread_id or "").strip()
    if agenda_thread_id_from_decision:
        return str(agenda_thread_id_from_decision)

    trigger_ref = str(decision.trigger_ref or "").strip()
    if not trigger_ref:
        return None

    # --- trigger payload を正規化し、agenda_thread_id だけを読む ---
    trigger = db.query(AutonomyTrigger).filter(AutonomyTrigger.trigger_id == str(trigger_ref)).one_or_none()
    if trigger is None:
        return None
    trigger_payload_obj = common_utils.json_loads_maybe(str(trigger.payload_json or "{}"))
    if not isinstance(trigger_payload_obj, dict):
        raise RuntimeError("autonomy_trigger.payload_json is not an object")
    agenda_thread_id = str(trigger_payload_obj.get("agenda_thread_id") or "").strip()
    if not agenda_thread_id:
        return None
    return str(agenda_thread_id)


def _select_agenda_thread_id_for_new_decision(
    *,
    trigger: AutonomyTrigger,
    deliberation_input: dict[str, Any],
    decision,
) -> str | None:
    """
    新規 ActionDecision 保存時に、対象 agenda_thread_id を解決する。

    方針:
        - trigger payload に `agenda_thread_id` があれば、それを最優先で採用する。
        - それが無い場合だけ、current_thought / agenda_threads の構造情報から選ぶ。
        - 意味推定はせず、active thread と next_action_type の一致だけを見る。
    """

    # --- do_action 以外は対象 thread を持たない ---
    if str(getattr(decision, "decision_outcome", "") or "") != "do_action":
        return None

    # --- trigger payload の明示指定があれば、その thread を使う ---
    trigger_payload_obj = common_utils.json_loads_maybe(str(trigger.payload_json or "{}"))
    if not isinstance(trigger_payload_obj, dict):
        raise RuntimeError("autonomy_trigger.payload_json is not an object")
    trigger_thread_id = str(trigger_payload_obj.get("agenda_thread_id") or "").strip()
    if trigger_thread_id:
        return str(trigger_thread_id)

    # --- current_thought / agenda_threads は deliberation 入力から読む ---
    current_thought_snapshot = deliberation_input.get("current_thought")
    if not isinstance(current_thought_snapshot, dict):
        current_thought_snapshot = {}
    agenda_threads_snapshot = deliberation_input.get("agenda_threads")
    if not isinstance(agenda_threads_snapshot, list):
        agenda_threads_snapshot = []

    # --- action_type が無い場合は特定できない ---
    action_type = str(getattr(decision, "action_type", "") or "").strip()
    if not action_type:
        return None

    # --- active thread を引いて、suggested / next_action の一致を優先する ---
    active_thread_id = str(current_thought_snapshot.get("active_thread_id") or "").strip()
    trigger_suggested_action_type = str(trigger_payload_obj.get("suggested_action_type") or "").strip()
    active_thread_row: dict[str, Any] | None = None
    ordered_threads = [dict(x) for x in list(agenda_threads_snapshot or []) if isinstance(x, dict)]
    if active_thread_id:
        for item in list(ordered_threads or []):
            if str(item.get("thread_id") or "").strip() == str(active_thread_id):
                active_thread_row = dict(item)
                break
    if isinstance(active_thread_row, dict):
        active_status = str(active_thread_row.get("status") or "").strip()
        active_next_action_type = str(active_thread_row.get("next_action_type") or "").strip()
        if active_status in {"active", "open", "blocked"}:
            if active_next_action_type and active_next_action_type == str(action_type):
                return str(active_thread_id)
            if trigger_suggested_action_type and trigger_suggested_action_type == str(action_type):
                return str(active_thread_id)

    # --- next_action_type が一意に一致する thread が1本だけなら採用する ---
    matching_thread_ids: list[str] = []
    seen_thread_ids: set[str] = set()
    for item in list(ordered_threads or []):
        thread_id = str(item.get("thread_id") or "").strip()
        thread_status = str(item.get("status") or "").strip()
        next_action_type = str(item.get("next_action_type") or "").strip()
        if not thread_id or thread_id in seen_thread_ids:
            continue
        if thread_status not in {"active", "open", "blocked"}:
            continue
        if next_action_type != str(action_type):
            continue
        seen_thread_ids.add(thread_id)
        matching_thread_ids.append(str(thread_id))
    if len(matching_thread_ids) == 1:
        return str(matching_thread_ids[0])

    return None


def _sync_agenda_thread_after_action_result(
    *,
    db,
    decision_id: str,
    result_event_id: int,
    result_id: str,
    action_type: str,
    result_status: str,
    result_payload: dict[str, Any],
    now_system_ts: int,
) -> None:
    """
    ActionResult 保存と同じトランザクションで agenda_thread を同期更新する。

    方針:
        - 後続 WritePlan の前に、debug/配信判定で見える正本を先に揃える。
        - 完了時の report candidate は action_result 規則で決める。
    """
    decision = db.query(ActionDecision).filter(ActionDecision.decision_id == str(decision_id)).one_or_none()
    if decision is None:
        return

    # --- trigger に紐づく agenda thread だけを更新対象にする ---
    agenda_thread_id = _resolve_agenda_thread_id_for_decision(
        db=db,
        decision=decision,
    )
    if not agenda_thread_id:
        return
    thread = db.query(AgendaThread).filter(AgendaThread.thread_id == str(agenda_thread_id)).one_or_none()
    if thread is None:
        return

    # --- 完了結果から thread 状態と共有候補を決める ---
    report_candidate = derive_report_candidate_for_action_result(
        action_type=str(action_type),
        result_status=str(result_status),
        result_payload=dict(result_payload),
    )
    result_status_norm = str(result_status)
    if result_status_norm == "failed":
        next_status = "blocked"
    elif result_status_norm == "partial":
        next_status = "open"
    elif result_status_norm == "no_effect":
        next_status = "stale"
    elif result_status_norm == "success":
        next_status = "satisfied"
    else:
        raise RuntimeError(f"unsupported result_status: {result_status_norm}")

    # --- thread 本体を同期更新する ---
    thread.source_event_id = int(result_event_id)
    thread.source_result_id = str(result_id)
    thread.status = str(next_status)
    thread.followup_due_at = None
    thread.last_progress_at = int(now_system_ts)
    thread.last_result_status = str(result_status_norm)
    thread.report_candidate_level = str(report_candidate["level"])
    thread.report_candidate_reason = (str(report_candidate["reason"]) if report_candidate["reason"] else None)
    if result_status_norm in {"no_effect", "success"}:
        thread.next_action_type = None
        thread.next_action_payload_json = "{}"
    thread.updated_at = int(now_system_ts)
    db.add(thread)


def _build_autonomy_message_report_focus(
    *,
    action_type: str,
    capability_name: str,
    result_status: str,
    backend: str | None,
    result_payload_for_render: dict[str, Any],
    runtime_blackboard_snapshot: dict[str, Any],
    current_thought_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    autonomy.message 用の「報告焦点（report_focus）」を構造情報だけで作る。

    方針:
        - 自然言語本文の意味推定は行わない。
        - result_payload / current_thought / runtime_blackboard の構造フィールドだけ使う。
        - LLM には「何を先に伝えるか」の候補を渡し、summary_text 依存を下げる。
    """

    # --- 入力の正規化 ---
    action_type_norm = str(action_type or "").strip()
    capability_norm = str(capability_name or "").strip()
    result_status_norm = str(result_status or "").strip()
    backend_norm = str(backend or "").strip() or None
    payload_obj = dict(result_payload_for_render or {})
    runtime_bb = dict(runtime_blackboard_snapshot or {})
    current_thought = (
        dict(current_thought_snapshot or {})
        if isinstance(current_thought_snapshot, dict)
        else {}
    )

    # --- attention_targets を集約（構造値のみ） ---
    attention_targets_raw: list[Any] = []
    if isinstance(runtime_bb.get("attention_targets"), list):
        attention_targets_raw.extend(list(runtime_bb.get("attention_targets") or []))
    if isinstance(current_thought.get("attention_targets"), list):
        attention_targets_raw.extend(list(current_thought.get("attention_targets") or []))

    attention_type_value_scores: dict[tuple[str, str], float] = {}

    # --- 重みの正規化 ---
    def _safe_weight(x: dict[str, Any]) -> float:
        try:
            return float(max(0.0, min(1.0, float(x.get("weight") or 0.0))))
        except Exception:  # noqa: BLE001
            return 0.0

    # --- 同一(type, value) の最大重みを残す ---
    for item in list(attention_targets_raw or []):
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip()
        v = str(item.get("value") or "").strip()
        if not t or not v:
            continue
        key = (str(t), str(v))
        attention_type_value_scores[key] = float(max(float(attention_type_value_scores.get(key, 0.0)), _safe_weight(item)))

    # --- 型別の重みマップへ再配置（後続の構造照合用） ---
    by_type_scores: dict[str, dict[str, float]] = {}
    for (t, v), w in dict(attention_type_value_scores).items():
        if t not in by_type_scores:
            by_type_scores[t] = {}
        by_type_scores[t][str(v)] = float(w)

    # --- 代表的な構造関心を取り出す ---
    interaction_mode = str(current_thought.get("interaction_mode") or "").strip()
    if interaction_mode not in {"observe", "support", "explore", "wait"}:
        interaction_mode = None
    research_interest_score = max(
        float((by_type_scores.get("world_capability") or {}).get("web_access") or 0.0),
        float((by_type_scores.get("world_entity_kind") or {}).get("web_research_query") or 0.0),
        float((by_type_scores.get("capability") or {}).get("web_access") or 0.0),
        float((by_type_scores.get("action_type") or {}).get("web_research") or 0.0),
    )
    action_alignment_score = float((by_type_scores.get("action_type") or {}).get(str(action_type_norm), 0.0))
    capability_alignment_score = float((by_type_scores.get("capability") or {}).get(str(capability_norm), 0.0))
    if capability_norm == "web_access":
        capability_alignment_score = max(
            float(capability_alignment_score),
            float((by_type_scores.get("world_capability") or {}).get("web_access") or 0.0),
        )

    # --- report_focus 候補の構造化（本文の意味推定はしない） ---
    focus_candidates: list[dict[str, Any]] = []

    # --- 候補を追加する共通処理 ---
    def _append_focus_candidate(*, kind: str, value: str, weight: float, source: str) -> None:
        v = str(value or "").strip()
        if not v:
            return
        focus_candidates.append(
            {
                "kind": str(kind),
                "value": str(v)[:240],
                "weight": float(max(0.0, min(1.0, float(weight)))),
                "source": str(source),
            }
        )

    # --- web_access の構造結果を最優先候補にする ---
    query_text = str(payload_obj.get("query") or "").strip()
    goal_text = str(payload_obj.get("goal") or "").strip()
    constraints_raw = payload_obj.get("constraints")
    findings_raw = payload_obj.get("findings")
    sources_raw = payload_obj.get("sources")
    notes_text = str(payload_obj.get("notes") or "").strip()
    if query_text:
        _append_focus_candidate(
            kind="query",
            value=query_text,
            weight=float(max(0.55, 0.70 + 0.20 * research_interest_score)),
            source="result_payload.query",
        )
    if goal_text:
        _append_focus_candidate(
            kind="goal",
            value=goal_text,
            weight=float(max(0.50, 0.66 + 0.18 * research_interest_score)),
            source="result_payload.goal",
        )
    if isinstance(findings_raw, list):
        for idx, item in enumerate(list(findings_raw or [])[:3]):
            _append_focus_candidate(
                kind="finding",
                value=str(item or ""),
                weight=float(max(0.45, 0.72 - 0.08 * idx + 0.12 * research_interest_score)),
                source="result_payload.findings",
            )
    if isinstance(sources_raw, list):
        for idx, src_obj in enumerate(list(sources_raw or [])[:3]):
            if not isinstance(src_obj, dict):
                continue
            title_text = str(src_obj.get("title") or "").strip()
            if not title_text:
                continue
            _append_focus_candidate(
                kind="source_title",
                value=title_text,
                weight=float(max(0.30, 0.52 - 0.06 * idx)),
                source="result_payload.sources",
            )
    if notes_text:
        _append_focus_candidate(
            kind="notes",
            value=notes_text,
            weight=0.40,
            source="result_payload.notes",
        )

    # --- agent_delegate / 汎用 backend の構造結果を候補にする ---
    task_instruction_text = str(payload_obj.get("task_instruction") or "").strip()
    if task_instruction_text:
        _append_focus_candidate(
            kind="task_instruction",
            value=task_instruction_text,
            weight=float(max(0.42, 0.58 + 0.12 * action_alignment_score)),
            source="result_payload.task_instruction",
        )

    details_obj = payload_obj.get("details")
    if isinstance(details_obj, dict):
        if "needs_attention" in details_obj:
            _append_focus_candidate(
                kind="needs_attention",
                value=("true" if bool(details_obj.get("needs_attention")) else "false"),
                weight=0.75 if bool(details_obj.get("needs_attention")) else 0.35,
                source="result_payload.details.needs_attention",
            )
        if "error_code" in details_obj:
            _append_focus_candidate(
                kind="error_code",
                value=str(details_obj.get("error_code") or ""),
                weight=0.85 if result_status_norm == "failed" else 0.40,
                source="result_payload.details.error_code",
            )
        if "error_message" in details_obj:
            _append_focus_candidate(
                kind="error_message",
                value=str(details_obj.get("error_message") or ""),
                weight=0.82 if result_status_norm == "failed" else 0.38,
                source="result_payload.details.error_message",
            )
        items_raw = details_obj.get("items")
        if isinstance(items_raw, list):
            for idx, row in enumerate(list(items_raw or [])[:4]):
                if not isinstance(row, dict):
                    continue
                item_kind = str(row.get("kind") or "").strip()
                item_priority = str(row.get("priority") or "").strip()
                item_label = ""
                for key in ["subject", "title", "label", "name"]:
                    item_label = str(row.get(key) or "").strip()
                    if item_label:
                        break
                if item_label:
                    suffix = f" ({item_kind})" if item_kind else ""
                    _append_focus_candidate(
                        kind="detail_item",
                        value=f"{item_label}{suffix}",
                        weight=float(max(0.40, 0.68 - 0.08 * idx + (0.08 if item_priority == 'high' else 0.0))),
                        source="result_payload.details.items",
                    )

    # --- capability/backend/action の構造情報も候補化（話題選択の足場） ---
    if capability_norm:
        _append_focus_candidate(
            kind="capability",
            value=capability_norm,
            weight=float(max(0.25, 0.30 + 0.40 * capability_alignment_score)),
            source="result.capability_name",
        )
    if action_type_norm:
        _append_focus_candidate(
            kind="action_type",
            value=action_type_norm,
            weight=float(max(0.25, 0.30 + 0.40 * action_alignment_score)),
            source="decision.action_type",
        )
    if backend_norm:
        _append_focus_candidate(
            kind="backend",
            value=backend_norm,
            weight=0.20,
            source="agent_job.backend",
        )

    # --- 重複をまとめて上位へ圧縮 ---
    focus_map: dict[tuple[str, str], dict[str, Any]] = {}
    for item in list(focus_candidates or []):
        kind = str(item.get("kind") or "").strip()
        value = str(item.get("value") or "").strip()
        if not kind or not value:
            continue
        key = (str(kind), str(value))
        prev = focus_map.get(key)
        if prev is None:
            focus_map[key] = dict(item)
            continue
        prev["weight"] = float(max(float(prev.get("weight") or 0.0), float(item.get("weight") or 0.0)))

    focus_candidates_sorted = sorted(
        list(focus_map.values()),
        key=lambda x: (-float(x.get("weight") or 0.0), str(x.get("kind") or ""), str(x.get("value") or "")),
    )[:8]

    # --- 構造的なサマリ（本文生成器への優先ルール用） ---
    fact_counts = {
        "constraints": int(len(list(constraints_raw or []))) if isinstance(constraints_raw, list) else 0,
        "findings": int(len(list(findings_raw or []))) if isinstance(findings_raw, list) else 0,
        "sources": int(len(list(sources_raw or []))) if isinstance(sources_raw, list) else 0,
    }

    # --- report_focus を返す ---
    return {
        "interaction_mode_hint": interaction_mode,
        "alignment": {
            "action_type": float(action_alignment_score),
            "capability": float(capability_alignment_score),
            "research_interest": float(research_interest_score),
        },
        "fact_counts": fact_counts,
        "focus_candidates": list(focus_candidates_sorted),
        "attention_targets_hint": [
            {
                "type": str(t),
                "value": str(v),
                "weight": float(w),
            }
            for (t, v), w in sorted(
                list(attention_type_value_scores.items()),
                key=lambda x: (-float(x[1]), str(x[0][0]), str(x[0][1])),
            )[:8]
        ],
    }


def _build_autonomy_message_render_input(
    *,
    persona_text: str,
    addon_text: str,
    second_person_label: str,
    mood_snapshot: dict[str, Any],
    confirmed_preferences: dict[str, Any],
    runtime_blackboard_snapshot: dict[str, Any],
    current_thought_snapshot: dict[str, Any] | None,
    decision: ActionDecision,
    console_delivery_obj: dict[str, Any],
    message_kind: str,
    delivery_mode: str,
    intent: Intent,
    result: ActionResult,
    result_payload: dict[str, Any],
    backend: str | None,
    agent_job_id: str | None,
) -> dict[str, Any]:
    """
    autonomy.message 人格発話生成用の構造化入力を作る。

    方針:
        - 文字列比較で意味を推定しない。
        - 事実（result/result_payload）と人格情報（persona/mood/preferences/runtime）を分けて渡す。
        - backend 生出力はそのまま見せず、入力事実としてのみ渡す。
    """

    # --- decision 内の監査JSONをdictへ戻す（不正なら空ではなくそのまま例外化したいので json_loads_maybe の結果を厳格に扱う） ---
    persona_influence_obj = common_utils.json_loads_maybe(str(decision.persona_influence_json or "{}"))
    if not isinstance(persona_influence_obj, dict):
        raise RuntimeError("decision.persona_influence_json is not an object")
    mood_influence_obj = common_utils.json_loads_maybe(str(decision.mood_influence_json or "{}"))
    if not isinstance(mood_influence_obj, dict):
        raise RuntimeError("decision.mood_influence_json is not an object")

    # --- result_payload を複製し、backend 生出力は本文生成入力から除外する ---
    result_payload_for_render = dict(result_payload or {})
    details_obj = result_payload_for_render.get("details")
    if isinstance(details_obj, dict):
        details_for_render = dict(details_obj)
        raw_output_text = str(details_for_render.get("raw_output_text") or "").strip()
        if raw_output_text:
            details_for_render.pop("raw_output_text", None)
            details_for_render["raw_output_text_meta"] = {
                "present": True,
                "chars": int(len(raw_output_text)),
            }
        result_payload_for_render["details"] = details_for_render

    # --- 長文フィールドを最小限に抑える（summary は補助情報へ格下げする） ---
    summary_text = str(result.summary_text or "").strip()
    if len(summary_text) > 400:
        summary_text = summary_text[:400]
    reason_text = str(decision.reason_text or "").strip()
    if len(reason_text) > 800:
        reason_text = reason_text[:800]

    # --- 報告焦点（report_focus）を構造情報から抽出して、summary 依存を下げる ---
    report_focus = _build_autonomy_message_report_focus(
        action_type=(str(intent.action_type) if intent.action_type is not None else ""),
        capability_name=(str(result.capability_name) if result.capability_name is not None else ""),
        result_status=str(result.result_status or ""),
        backend=(str(backend) if backend else None),
        result_payload_for_render=dict(result_payload_for_render),
        runtime_blackboard_snapshot=dict(runtime_blackboard_snapshot or {}),
        current_thought_snapshot=(
            dict(current_thought_snapshot)
            if isinstance(current_thought_snapshot, dict)
            else None
        ),
    )

    # --- render 入力（構造化JSON） ---
    return {
        "persona": {
            "second_person_label": str(second_person_label or "").strip() or "あなた",
            "persona_text": str(persona_text or "").strip(),
            "addon_text": str(addon_text or "").strip(),
        },
        "mood": dict(mood_snapshot or {}),
        "confirmed_preferences": dict(confirmed_preferences or {}),
        "runtime_blackboard": dict(runtime_blackboard_snapshot or {}),
        "current_thought": (
            dict(current_thought_snapshot)
            if isinstance(current_thought_snapshot, dict)
            else None
        ),
        "delivery": {
            "mode": str(delivery_mode),
            "message_kind": str(message_kind),
            "console_delivery": dict(console_delivery_obj or {}),
        },
        "report_focus": dict(report_focus or {}),
        "decision": {
            "decision_id": str(decision.decision_id),
            "action_type": (str(intent.action_type) if intent.action_type is not None else None),
            "reason": str(reason_text),
            "persona_influence": dict(persona_influence_obj),
            "mood_influence": dict(mood_influence_obj),
        },
        "result": {
            "result_id": str(result.result_id),
            "result_status": str(result.result_status),
            "capability_name": (str(result.capability_name) if result.capability_name is not None else None),
            "summary_text": str(summary_text),
            "result_payload": dict(result_payload_for_render),
        },
        "agent_job": {
            "backend": (str(backend) if backend else None),
            "job_id": (str(agent_job_id) if agent_job_id else None),
        },
    }


def _render_autonomy_message_text(*, render_input: dict[str, Any]) -> str:
    """
    autonomy.message の人格発話本文を生成する。

    方針:
        - `ActionResult.summary_text` を素通ししない。
        - 失敗時は呼び出し元で skip する（raw summary フォールバック禁止）。
    """

    # --- 現在設定から人格/LLM設定を取得し、worker 用 LLM client を生成 ---
    from cocoro_ghost.app_bootstrap.dependencies import get_llm_client

    cfg = get_config_store().config
    llm_client = get_llm_client()

    # --- 専用 prompt で人格発話を生成（Web検索はSYNC_CONVERSATION以外で無効） ---
    system_prompt = prompt_builders.autonomy_message_render_system_prompt(
        second_person_label=str(getattr(cfg, "second_person_label", "") or ""),
    )
    user_prompt = prompt_builders.autonomy_message_render_user_prompt(
        render_input=dict(render_input or {}),
    )
    resp = llm_client.generate_reply_response(
        system_prompt=system_prompt,
        conversation=[{"role": "user", "content": str(user_prompt)}],
        purpose=LlmRequestPurpose.ASYNC_AUTONOMY_MESSAGE_RENDER,
        stream=False,
    )

    # --- 本文を取り出して返す（空は失敗扱い） ---
    message_text = str(llm_client.response_content(resp) or "").strip()
    if not message_text:
        raise RuntimeError("autonomy.message render returned empty content")
    return str(message_text)


def _emit_autonomy_console_events_for_action_result(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    result_id: str,
    activity_state_override: str | None = None,
) -> None:
    """
    ActionResult を起点に autonomy.activity / autonomy.message を発行する。

    方針:
        - `autonomy.activity` は監視用イベントとして配信する。
        - `autonomy.message` は AI人格の発話として events に保存してから配信する。
        - publishした時点で発話成立とする。
    """

    # --- 参照IDを正規化 ---
    result_id_norm = str(result_id or "").strip()
    if not result_id_norm:
        return

    # --- publish用スナップショットをトランザクション内で組み立てる ---
    activity_publish: dict[str, Any] | None = None
    message_emit_plan: dict[str, Any] | None = None

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        result = db.query(ActionResult).filter(ActionResult.result_id == str(result_id_norm)).one_or_none()
        if result is None:
            return

        intent = db.query(Intent).filter(Intent.intent_id == str(result.intent_id)).one_or_none()
        decision = db.query(ActionDecision).filter(ActionDecision.decision_id == str(result.decision_id)).one_or_none()
        if intent is None or decision is None:
            return

        # --- console_delivery を厳格に読む（不正データは設計不整合として例外化） ---
        console_delivery_raw = common_utils.json_loads_maybe(str(decision.console_delivery_json or ""))
        console_delivery_obj = parse_console_delivery(console_delivery_raw)

        # --- agent_delegate 結果なら payload から backend / agent_job_id を拾う ---
        result_payload_obj = common_utils.json_loads_maybe(str(result.result_payload_json or "{}"))
        if not isinstance(result_payload_obj, dict):
            raise RuntimeError("action_result.result_payload_json is not an object")
        result_payload = dict(result_payload_obj)
        agent_job_id = None
        backend = None
        if str(intent.action_type or "") == "agent_delegate":
            agent_job_id = str(result_payload.get("agent_job_id") or "").strip() or None
            backend = str(result_payload.get("backend") or "").strip() or None

        # --- 完了時の activity は常に配信し、監視UIの正本にする ---
        activity_publish = {
            "event_id": int(result.event_id),
            "phase": "execution",
            "state": (str(activity_state_override) if activity_state_override else "completed"),
            "action_type": (str(intent.action_type) if intent.action_type is not None else None),
            "capability": (str(result.capability_name) if result.capability_name is not None else None),
            "backend": backend,
            "result_status": str(result.result_status),
            "summary_text": (str(result.summary_text) if result.summary_text is not None else None),
            "decision_id": str(decision.decision_id),
            "intent_id": str(intent.intent_id),
            "result_id": str(result.result_id),
            "agent_job_id": agent_job_id,
            "goal_id": (str(intent.goal_id) if intent.goal_id is not None else None),
        }

        # --- 完了時の delivery は action_result 規則で決める ---
        report_candidate = derive_report_candidate_for_action_result(
            action_type=(str(intent.action_type) if intent.action_type is not None else ""),
            result_status=str(result.result_status),
            result_payload=dict(result_payload),
        )
        terminal_mode = resolve_delivery_mode_from_report_candidate_level(
            report_candidate.get("level"),
        )

        # --- autonomy.message は post-result delivery が notify/chat のときだけ作る ---
        if str(terminal_mode) in {"notify", "chat"}:
            message_kind = resolve_message_kind_for_action_result(
                result_status=str(result.result_status),
            )
            cfg = get_config_store().config
            mood_snapshot = _load_recent_mood_snapshot(db)
            confirmed_preferences_snapshot = _load_confirmed_preferences_snapshot_for_deliberation(db)
            current_thought_snapshot = _load_current_thought_snapshot_for_deliberation(db)
            runtime_blackboard_snapshot = get_runtime_blackboard().snapshot()
            if not isinstance(runtime_blackboard_snapshot, dict):
                runtime_blackboard_snapshot = {}
            render_input = _build_autonomy_message_render_input(
                persona_text=str(getattr(cfg, "persona_text", "") or ""),
                addon_text=str(getattr(cfg, "addon_text", "") or ""),
                second_person_label=str(getattr(cfg, "second_person_label", "") or ""),
                mood_snapshot=dict(mood_snapshot or {}),
                confirmed_preferences=dict(confirmed_preferences_snapshot or {}),
                runtime_blackboard_snapshot=dict(runtime_blackboard_snapshot or {}),
                current_thought_snapshot=(
                    dict(current_thought_snapshot)
                    if isinstance(current_thought_snapshot, dict)
                    else None
                ),
                decision=decision,
                console_delivery_obj=dict(console_delivery_obj),
                message_kind=str(message_kind),
                delivery_mode=str(terminal_mode),
                intent=intent,
                result=result,
                result_payload=dict(result_payload),
                backend=backend,
                agent_job_id=agent_job_id,
            )

            # --- message 保存/配信のためのメタ情報だけ保持（本文生成はDB外で行う） ---
            message_emit_plan = {
                "delivery_mode": str(terminal_mode),
                "message_kind": str(message_kind),
                "action_type": (str(intent.action_type) if intent.action_type is not None else None),
                "capability": (str(result.capability_name) if result.capability_name is not None else None),
                "backend": backend,
                "decision_id": str(decision.decision_id),
                "intent_id": str(intent.intent_id),
                "result_id": str(result.result_id),
                "agent_job_id": agent_job_id,
                "render_input": render_input,
            }

    # --- DB確定後に監視イベントを配信 ---
    if isinstance(activity_publish, dict):
        _publish_autonomy_activity(
            event_id=int(activity_publish.get("event_id") or 0),
            phase=str(activity_publish.get("phase") or "execution"),
            state=str(activity_publish.get("state") or "completed"),
            action_type=(str(activity_publish.get("action_type")) if activity_publish.get("action_type") else None),
            capability=(str(activity_publish.get("capability")) if activity_publish.get("capability") else None),
            backend=(str(activity_publish.get("backend")) if activity_publish.get("backend") else None),
            result_status=(str(activity_publish.get("result_status")) if activity_publish.get("result_status") else None),
            summary_text=(str(activity_publish.get("summary_text")) if activity_publish.get("summary_text") else None),
            decision_id=(str(activity_publish.get("decision_id")) if activity_publish.get("decision_id") else None),
            intent_id=(str(activity_publish.get("intent_id")) if activity_publish.get("intent_id") else None),
            result_id=(str(activity_publish.get("result_id")) if activity_publish.get("result_id") else None),
            agent_job_id=(str(activity_publish.get("agent_job_id")) if activity_publish.get("agent_job_id") else None),
            goal_id=(str(activity_publish.get("goal_id")) if activity_publish.get("goal_id") else None),
        )

    # --- 発話イベントは DB外で生成 -> 保存 -> publish する（生成失敗時はフォールバックしない） ---
    if isinstance(message_emit_plan, dict):
        try:
            render_input = dict(message_emit_plan.get("render_input") or {})
            message_text = _render_autonomy_message_text(render_input=render_input).strip()
            if not message_text:
                raise RuntimeError("autonomy.message render returned empty content")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "autonomy.message render skipped result_id=%s error=%s",
                str(message_emit_plan.get("result_id") or ""),
                str(exc),
            )
            return

        # --- 発話イベントを保存し、通常イベント同様に後続ジョブへ流す ---
        now_domain_ts = _now_domain_utc_ts()
        now_system_ts = _now_utc_ts()
        message_event_id: int | None = None
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            message_event = Event(
                created_at=int(now_domain_ts),
                updated_at=int(now_domain_ts),
                searchable=1,
                client_id=None,
                source="autonomy_message",
                user_text=None,
                assistant_text=str(message_text),
                entities_json="[]",
                client_context_json=common_utils.json_dumps(
                    {
                        "decision_id": str(message_emit_plan.get("decision_id") or ""),
                        "intent_id": str(message_emit_plan.get("intent_id") or ""),
                        "result_id": str(message_emit_plan.get("result_id") or ""),
                        "action_type": (str(message_emit_plan.get("action_type")) if message_emit_plan.get("action_type") else None),
                        "capability": (str(message_emit_plan.get("capability")) if message_emit_plan.get("capability") else None),
                        "backend": (str(message_emit_plan.get("backend")) if message_emit_plan.get("backend") else None),
                        "agent_job_id": (str(message_emit_plan.get("agent_job_id")) if message_emit_plan.get("agent_job_id") else None),
                        "message_kind": str(message_emit_plan.get("message_kind") or "report"),
                        "delivery_mode": str(message_emit_plan.get("delivery_mode") or "chat"),
                    }
                ),
            )
            db.add(message_event)
            db.flush()
            message_event_id = int(message_event.event_id)

            _enqueue_standard_post_event_jobs(
                db=db,
                event_id=int(message_event_id),
                now_system_ts=int(now_system_ts),
            )

        # --- 保存済みevent_idで publish（これを発話成立とみなす） ---
        if message_event_id is not None:
            event_stream.publish(
                type="autonomy.message",
                event_id=int(message_event_id),
                data={
                    "message": str(message_text),
                    "message_kind": str(message_emit_plan.get("message_kind") or "report"),
                    "delivery_mode": str(message_emit_plan.get("delivery_mode") or "chat"),
                    "action_type": (str(message_emit_plan.get("action_type")) if message_emit_plan.get("action_type") else None),
                    "capability": (str(message_emit_plan.get("capability")) if message_emit_plan.get("capability") else None),
                    "backend": (str(message_emit_plan.get("backend")) if message_emit_plan.get("backend") else None),
                    "decision_id": (str(message_emit_plan.get("decision_id")) if message_emit_plan.get("decision_id") else None),
                    "intent_id": (str(message_emit_plan.get("intent_id")) if message_emit_plan.get("intent_id") else None),
                    "result_id": (str(message_emit_plan.get("result_id")) if message_emit_plan.get("result_id") else None),
                    "agent_job_id": (str(message_emit_plan.get("agent_job_id")) if message_emit_plan.get("agent_job_id") else None),
                },
                target_client_id=None,
            )


def _load_recent_mood_snapshot(db) -> dict[str, Any]:
    """Deliberation入力用の Mood スナップショットを返す。"""

    # --- long_mood_state（最新1件） ---
    long_mood = (
        db.query(State)
        .filter(State.searchable == 1)
        .filter(State.kind == "long_mood_state")
        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
        .first()
    )
    long_mood_payload = None
    if long_mood is not None:
        mood_payload_obj = common_utils.json_loads_maybe(str(long_mood.payload_json or ""))
        long_mood_payload = {
            "state_id": int(long_mood.state_id),
            "body_text": str(long_mood.body_text),
            "payload": mood_payload_obj,
            "last_confirmed_at": int(long_mood.last_confirmed_at),
        }

    # --- 直近 affect のVAD平均 ---
    affects = db.query(EventAffect).order_by(EventAffect.created_at.desc()).limit(12).all()
    if not affects:
        return {
            "long_mood_state": long_mood_payload,
            "recent_vad_average": {"v": 0.0, "a": 0.0, "d": 0.0},
        }
    v = sum(float(a.vad_v) for a in affects) / float(len(affects))
    a = sum(float(x.vad_a) for x in affects) / float(len(affects))
    d = sum(float(x.vad_d) for x in affects) / float(len(affects))
    return {
        "long_mood_state": long_mood_payload,
        "recent_vad_average": {"v": float(v), "a": float(a), "d": float(d)},
    }


def _load_confirmed_preferences_snapshot_for_deliberation(db) -> dict[str, Any]:
    """
    Deliberation入力用の confirmed preferences スナップショットを返す。

    方針:
        - 文字列比較で意味推定しない。
        - 構造化済みの confirmed preference を短い形で渡す。
    """

    # --- 返却形は固定（欠損時も同じ構造） ---
    out: dict[str, Any] = {
        "food": {"like": [], "dislike": []},
        "topic": {"like": [], "dislike": []},
        "style": {"like": [], "dislike": []},
    }

    # --- DBから confirmed のみ取得（新しい順） ---
    rows = (
        db.query(
            UserPreference.domain,
            UserPreference.polarity,
            UserPreference.subject_raw,
            UserPreference.note,
            UserPreference.confirmed_at,
            UserPreference.last_seen_at,
            UserPreference.id,
        )
        .filter(UserPreference.status == "confirmed")
        .order_by(UserPreference.confirmed_at.desc(), UserPreference.last_seen_at.desc(), UserPreference.id.desc())
        .all()
    )

    # --- domain/polarity ごとに少数へ圧縮 ---
    cap_per_bucket = 6
    counts: dict[tuple[str, str], int] = {}
    for r in list(rows or []):
        domain = str(r[0] or "").strip()
        polarity = str(r[1] or "").strip()
        subject = str(r[2] or "").strip()
        note = str(r[3] or "").strip()
        if domain not in {"food", "topic", "style"}:
            continue
        if polarity not in {"like", "dislike"}:
            continue
        if not subject:
            continue
        key = (domain, polarity)
        if int(counts.get(key, 0)) >= int(cap_per_bucket):
            continue
        out[domain][polarity].append(
            {
                "subject": str(subject[:120]),
                "note": (str(note[:240]) if note else None),
            }
        )
        counts[key] = int(counts.get(key, 0)) + 1

    return out


def _load_current_thought_snapshot_for_deliberation(db) -> dict[str, Any] | None:
    """
    Deliberation / autonomy.message 再生成用の current_thought_state スナップショットを返す。

    方針:
        - 構造化 payload を正として扱う。
        - 文字列比較で意味推定しない。
    """

    st = (
        db.query(State)
        .filter(State.searchable == 1)
        .filter(State.kind == "current_thought_state")
        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
        .first()
    )
    if st is None:
        return None

    payload_obj = common_utils.json_loads_maybe(str(st.payload_json or ""))
    if not isinstance(payload_obj, dict):
        payload_obj = {}

    attention_targets_out: list[dict[str, Any]] = []
    attention_targets_raw = payload_obj.get("attention_targets")
    if isinstance(attention_targets_raw, list):
        for item in list(attention_targets_raw):
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("type") or "").strip()
            value = str(item.get("value") or "").strip()
            if not target_type or not value:
                continue
            attention_targets_out.append(
                {
                    "type": str(target_type),
                    "value": str(value),
                    "weight": float(item.get("weight") or 0.0),
                    "updated_at": (int(item.get("updated_at")) if item.get("updated_at") is not None else None),
                }
            )
            if len(attention_targets_out) >= 24:
                break

    updated_from_event_ids = payload_obj.get("updated_from_event_ids")
    if not isinstance(updated_from_event_ids, list):
        updated_from_event_ids = []

    return {
        "state_id": int(st.state_id),
        "kind": str(st.kind),
        "body_text": str(st.body_text or ""),
        "interaction_mode": str(payload_obj.get("interaction_mode") or "").strip() or None,
        "active_thread_id": str(payload_obj.get("active_thread_id") or "").strip() or None,
        "focus_summary": str(payload_obj.get("focus_summary") or "").strip() or None,
        "next_candidate_action": (
            dict(payload_obj.get("next_candidate_action") or {})
            if isinstance(payload_obj.get("next_candidate_action"), dict)
            else None
        ),
        "talk_candidate": (
            dict(payload_obj.get("talk_candidate") or {})
            if isinstance(payload_obj.get("talk_candidate"), dict)
            else None
        ),
        "attention_targets": attention_targets_out,
        "updated_from_event_ids": [
            int(x) for x in list(updated_from_event_ids) if isinstance(x, (int, float)) and int(x) > 0
        ][:20],
        "updated_from": (payload_obj.get("updated_from") if isinstance(payload_obj.get("updated_from"), dict) else None),
        "confidence": float(st.confidence),
        "last_confirmed_at": int(st.last_confirmed_at),
    }


def _load_agenda_threads_snapshot_for_deliberation(db) -> list[dict[str, Any]]:
    """Deliberation 用の agenda_threads スナップショットを返す。"""

    rows = (
        db.query(AgendaThread)
        .filter(AgendaThread.status.in_(["active", "open", "blocked"]))
        .order_by(AgendaThread.updated_at.desc())
        .limit(8)
        .all()
    )

    status_rank = {
        "active": 0,
        "open": 1,
        "blocked": 2,
    }
    ordered_rows = sorted(
        list(rows or []),
        key=lambda row: (
            int(status_rank.get(str(row.status or ""), 9)),
            -int(row.priority or 0),
            -int(row.updated_at or 0),
            str(row.thread_id or ""),
        ),
    )[:8]

    out: list[dict[str, Any]] = []
    for row in list(ordered_rows or []):
        payload_obj = common_utils.json_loads_maybe(str(row.next_action_payload_json or "{}"))
        if not isinstance(payload_obj, dict):
            payload_obj = {}
        out.append(
            {
                "thread_id": str(row.thread_id),
                "thread_key": str(row.thread_key),
                "kind": str(row.kind),
                "topic": str(row.topic),
                "goal": str(row.goal or "").strip() or None,
                "status": str(row.status),
                "priority": int(row.priority),
                "next_action_type": str(row.next_action_type or "").strip() or None,
                "next_action_payload": dict(payload_obj),
                "followup_due_at": (int(row.followup_due_at) if row.followup_due_at is not None else None),
                "last_result_status": str(row.last_result_status or "").strip() or None,
                "report_candidate_level": str(row.report_candidate_level),
            }
        )
    return out


def _build_persona_deliberation_focus(
    *,
    trigger: AutonomyTrigger,
    confirmed_preferences: dict[str, Any],
    runtime_blackboard_snapshot: dict[str, Any],
    current_thought_snapshot: dict[str, Any] | None,
    agenda_threads_snapshot: list[dict[str, Any]],
    active_goal_ids: list[str],
) -> dict[str, Any]:
    """
    Deliberation 材料選別用のフォーカス情報を構築する。

    方針:
        - 自然言語本文の意味推定は行わない。
        - 構造化済みの preferences/runtime/current_thought/agenda 情報だけで、並び順と件数のヒントを作る。
    """

    # --- runtime の短期継続状態 ---
    active_intent_ids_raw = runtime_blackboard_snapshot.get("active_intent_ids")
    active_intent_ids: list[str] = []
    if isinstance(active_intent_ids_raw, list):
        seen_intent_ids: set[str] = set()
        for x in list(active_intent_ids_raw):
            sid = str(x or "").strip()
            if not sid or sid in seen_intent_ids:
                continue
            seen_intent_ids.add(sid)
            active_intent_ids.append(sid)

    trigger_counts = runtime_blackboard_snapshot.get("trigger_counts")
    if not isinstance(trigger_counts, dict):
        trigger_counts = {}
    intent_counts = runtime_blackboard_snapshot.get("intent_counts")
    if not isinstance(intent_counts, dict):
        intent_counts = {}

    # --- runtime attention_targets + current_thought.attention_targets を使う ---
    attention_targets_raw: list[Any] = []
    if isinstance(runtime_blackboard_snapshot.get("attention_targets"), list):
        attention_targets_raw.extend(list(runtime_blackboard_snapshot.get("attention_targets") or []))
    if isinstance(current_thought_snapshot, dict) and isinstance(current_thought_snapshot.get("attention_targets"), list):
        attention_targets_raw.extend(list(current_thought_snapshot.get("attention_targets") or []))
    attention_targets: list[dict[str, Any]] = []
    for item in list(attention_targets_raw or []):
        if not isinstance(item, dict):
            continue
        attention_targets.append(dict(item))

    # --- 重み付き優先度マップ（current_thought / runtime の attention_targets を実際の選別に使う） ---
    priority_state_kind_scores: dict[str, float] = {}
    priority_goal_id_scores: dict[str, float] = {}
    priority_action_type_scores: dict[str, float] = {}
    priority_capability_scores: dict[str, float] = {}
    priority_event_source_scores: dict[str, float] = {}
    priority_goal_type_scores: dict[str, float] = {}

    # --- 後方互換/デバッグ用に、最終的な優先リストも維持する ---
    priority_state_kinds: list[str] = []
    priority_goal_ids: list[str] = []
    priority_action_types: list[str] = []
    priority_capabilities: list[str] = []
    priority_event_sources: list[str] = []
    priority_goal_types: list[str] = []

    # --- capability と action_type の構造対応表 ---
    capability_to_action_types = {
        "web_access": ["web_research"],
        "vision_perception": ["observe_screen", "observe_camera"],
        "schedule_alarm": ["schedule_action"],
        "device_control": ["device_action"],
        "mobility_move": ["move_to"],
        "agent_delegate": ["agent_delegate"],
    }

    # --- 重みを安全に読む（構造値のみ使用） ---
    def _target_weight(item_obj: dict[str, Any]) -> float:
        try:
            return float(max(0.0, min(1.0, float(item_obj.get("weight") or 0.0))))
        except Exception:  # noqa: BLE001
            return 0.0

    # --- スコアへ加算してから、最終的に優先リスト化する ---
    def _add_score(dst: dict[str, float], key: str, weight: float) -> None:
        k = str(key or "").strip()
        if not k:
            return
        dst[k] = float(max(float(dst.get(k, 0.0)), float(weight)))

    for item in attention_targets:
        t = str(item.get("type") or "").strip()
        v = str(item.get("value") or "").strip()
        w = _target_weight(item)
        if not v:
            continue
        if t in {"state_kind", "kind"}:
            _add_score(priority_state_kind_scores, v, w)
            continue
        if t == "goal_id":
            _add_score(priority_goal_id_scores, v, w)
            continue
        if t == "action_type":
            _add_score(priority_action_type_scores, v, w)
            continue
        if t == "capability":
            _add_score(priority_capability_scores, v, w)
            continue
        if t == "event_source":
            _add_score(priority_event_source_scores, v, w)
            continue

        # --- world_model_items 由来ターゲットを Deliberation 選別へ橋渡しする ---
        if t == "world_capability":
            _add_score(priority_capability_scores, v, w)
            for at in list(capability_to_action_types.get(v, []) or []):
                _add_score(priority_action_type_scores, str(at), float(max(0.0, w - 0.05)))
            if v == "web_access":
                _add_score(priority_goal_type_scores, "research", w)
            continue

        if t == "world_entity_kind":
            # --- web_research_query は構造的に research/web_research へ寄せられる ---
            if v == "web_research_query":
                _add_score(priority_action_type_scores, "web_research", w)
                _add_score(priority_capability_scores, "web_access", w)
                _add_score(priority_goal_type_scores, "research", w)
            continue

        if t == "world_query":
            # --- query文字列の意味推定はしない。検索系関心がある事実だけ使う。 ---
            _add_score(priority_action_type_scores, "web_research", w)
            _add_score(priority_capability_scores, "web_access", w)
            _add_score(priority_goal_type_scores, "research", w)
            continue

        if t == "world_goal":
            # --- goal本文の意味推定はしない。research継続の構造ヒントとして扱う。 ---
            _add_score(priority_goal_type_scores, "research", w)
            continue

    # --- agenda_threads の構造値も優先度へ反映する ---
    agenda_threads = [dict(x) for x in list(agenda_threads_snapshot or []) if isinstance(x, dict)]
    has_due_agenda_action = False
    has_active_agenda = False
    for item in list(agenda_threads or []):
        thread_status = str(item.get("status") or "").strip()
        if thread_status not in {"active", "open", "blocked"}:
            continue
        has_active_agenda = True
        status_weight = 1.0 if thread_status == "active" else (0.75 if thread_status == "open" else 0.55)
        next_action_type = str(item.get("next_action_type") or "").strip()
        if next_action_type:
            has_due_agenda_action = True
            _add_score(priority_action_type_scores, next_action_type, status_weight)
            if next_action_type == "web_research":
                _add_score(priority_capability_scores, "web_access", float(max(0.0, status_weight - 0.05)))
                _add_score(priority_goal_type_scores, "research", status_weight)
        thread_kind = str(item.get("kind") or "").strip()
        if thread_kind == "research":
            _add_score(priority_goal_type_scores, "research", status_weight)
        elif thread_kind == "observation" and next_action_type in {"observe_screen", "observe_camera"}:
            _add_score(priority_capability_scores, "vision_perception", float(max(0.0, status_weight - 0.05)))

    # --- スコア上位順に優先リスト化（既存の再並び替え関数互換） ---
    def _top_keys(score_map: dict[str, float], *, cap: int) -> list[str]:
        ordered = sorted(
            [(str(k), float(v)) for k, v in dict(score_map or {}).items() if str(k or "").strip()],
            key=lambda x: (-float(x[1]), str(x[0])),
        )
        return [str(k) for k, _ in ordered[: int(cap)]]

    priority_state_kinds = _top_keys(priority_state_kind_scores, cap=6)
    priority_goal_ids = _top_keys(priority_goal_id_scores, cap=8)
    priority_action_types = _top_keys(priority_action_type_scores, cap=8)
    priority_capabilities = _top_keys(priority_capability_scores, cap=8)
    priority_event_sources = _top_keys(priority_event_source_scores, cap=8)
    priority_goal_types = _top_keys(priority_goal_type_scores, cap=4)

    # --- preferences の構造量（意味推定せず件数だけ使う） ---
    topic_like_count = len(list(((confirmed_preferences.get("topic") or {}).get("like") or []))) if isinstance(confirmed_preferences, dict) else 0
    topic_dislike_count = len(list(((confirmed_preferences.get("topic") or {}).get("dislike") or []))) if isinstance(confirmed_preferences, dict) else 0
    style_like_count = len(list(((confirmed_preferences.get("style") or {}).get("like") or []))) if isinstance(confirmed_preferences, dict) else 0
    has_research_interest = bool(priority_goal_types) and ("research" in {str(x) for x in list(priority_goal_types or [])})
    has_active_focus = bool(priority_action_types or priority_capabilities or priority_goal_types or has_active_agenda)

    # --- interaction mode を構造値から推定（VADは使わない） ---
    # --- current_thought.interaction_mode を最優先ヒントとして使う（構造値のみ） ---
    interaction_mode_hint = ""
    if isinstance(current_thought_snapshot, dict):
        interest_mode_raw = str(current_thought_snapshot.get("interaction_mode") or "").strip()
        if interest_mode_raw in {"observe", "support", "explore", "wait"}:
            interaction_mode_hint = str(interest_mode_raw)
    if not interaction_mode_hint:
        interaction_mode_hint = "observe"
        if len(active_intent_ids) >= 2:
            interaction_mode_hint = "support"
        elif bool(has_due_agenda_action) or bool(has_active_focus) or topic_like_count > 0:
            interaction_mode_hint = "explore"
        elif not active_intent_ids and str(trigger.trigger_type or "").strip() in {"heartbeat", "time", "time_routine"}:
            interaction_mode_hint = "wait"

    # --- 継続重視度 ---
    continuity_bias = "high" if active_intent_ids else "medium"
    if not active_intent_ids and str(trigger.trigger_type or "").strip() in {"heartbeat", "time_routine"}:
        continuity_bias = "low"

    # --- Deliberation 入力件数（収集後の最終件数） ---
    limits = {
        "events": 24,
        "states": 24,
        "goals": 8,
        "intents": 8,
    }
    if continuity_bias == "high":
        limits["events"] = 18
        limits["states"] = 20
        limits["goals"] = 10
        limits["intents"] = 12
    if interaction_mode_hint == "wait":
        limits["events"] = min(int(limits["events"]), 16)
        limits["states"] = min(int(limits["states"]), 20)
    # --- current_thought 由来の検索/調査関心でも explore 枠を少し広げる ---
    if interaction_mode_hint == "explore" and (topic_like_count > 0 or has_research_interest):
        limits["events"] = min(int(limits["events"]) + 4, 28)
        limits["states"] = min(int(limits["states"]) + 2, 26)

    # --- fetch件数（再並び替え用に少し多めに取る） ---
    fetch_limits = {
        "events": max(int(limits["events"]) + 12, 24),
        "states": max(int(limits["states"]) + 12, 24),
        "goals": max(int(limits["goals"]) + 4, 8),
        "intents": max(int(limits["intents"]) + 4, 8),
    }

    return {
        "interaction_mode_hint": str(interaction_mode_hint),
        "continuity_bias": str(continuity_bias),
        "active_intent_ids": list(active_intent_ids),
        "active_goal_ids": list(active_goal_ids),
        "priority_state_kinds": list(priority_state_kinds),
        "priority_goal_ids": list(priority_goal_ids),
        "priority_action_types": list(priority_action_types),
        "priority_capabilities": list(priority_capabilities),
        "priority_event_sources": list(priority_event_sources),
        "priority_goal_types": list(priority_goal_types),
        "priority_scores": {
            "state_kind": dict(priority_state_kind_scores),
            "goal_id": dict(priority_goal_id_scores),
            "action_type": dict(priority_action_type_scores),
            "capability": dict(priority_capability_scores),
            "event_source": dict(priority_event_source_scores),
            "goal_type": dict(priority_goal_type_scores),
        },
        "preferences_bias": {
            "topic_like_count": int(topic_like_count),
            "topic_dislike_count": int(topic_dislike_count),
            "style_like_count": int(style_like_count),
        },
        "runtime_counts": {
            "trigger_counts": dict(trigger_counts),
            "intent_counts": dict(intent_counts),
        },
        "limits": {
            "final": dict(limits),
            "fetch": dict(fetch_limits),
        },
    }


def _reorder_deliberation_intents(intents: list[dict[str, Any]], *, focus: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Deliberation 用 intents を構造情報ベースで再整列する。
    """

    # --- active intent を先頭へ寄せる ---
    active_intent_ids = {str(x) for x in list(focus.get("active_intent_ids") or [])}
    priority_action_types = {str(x) for x in list(focus.get("priority_action_types") or []) if str(x or "").strip()}
    priority_capabilities = {str(x) for x in list(focus.get("priority_capabilities") or []) if str(x or "").strip()}
    priority_scores = focus.get("priority_scores") if isinstance(focus, dict) else {}
    if not isinstance(priority_scores, dict):
        priority_scores = {}
    action_type_scores = priority_scores.get("action_type")
    capability_scores = priority_scores.get("capability")
    if not isinstance(action_type_scores, dict):
        action_type_scores = {}
    if not isinstance(capability_scores, dict):
        capability_scores = {}
    status_rank = {"running": 0, "queued": 1, "blocked": 2}

    # --- action_type から capability を構造的に対応付ける ---
    action_type_to_capability = {
        "web_research": "web_access",
        "observe_screen": "vision_perception",
        "observe_camera": "vision_perception",
        "schedule_action": "schedule_alarm",
        "device_action": "device_control",
        "move_to": "mobility_move",
        "agent_delegate": "agent_delegate",
    }

    def _sort_key(it: dict[str, Any]) -> tuple[int, int, int, float, float, int, str]:
        iid = str(it.get("intent_id") or "")
        status = str(it.get("status") or "")
        action_type = str(it.get("action_type") or "")
        capability = str(action_type_to_capability.get(action_type, ""))
        priority = int(it.get("priority") or 0)
        scheduled_at = it.get("scheduled_at")
        scheduled_rank = int(scheduled_at) if scheduled_at is not None else 2**31 - 1
        action_score = float(action_type_scores.get(action_type) or 0.0)
        capability_score = float(capability_scores.get(capability) or 0.0)
        return (
            0 if iid in active_intent_ids else 1,
            0 if action_type in priority_action_types else 1,
            0 if (capability and capability in priority_capabilities) else 1,
            -float(action_score),
            -float(capability_score),
            int(status_rank.get(status, 9)),
            -int(priority),
            f"{scheduled_rank:010d}",
        )

    return list(sorted(list(intents or []), key=_sort_key))


def _reorder_deliberation_goals(goals: list[dict[str, Any]], *, focus: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Deliberation 用 goals を構造情報ベースで再整列する。
    """

    # --- active intents が参照中の goal を優先 ---
    active_goal_ids = {str(x) for x in list(focus.get("active_goal_ids") or []) if str(x or "").strip()}
    priority_goal_ids = {str(x) for x in list(focus.get("priority_goal_ids") or []) if str(x or "").strip()}
    priority_goal_types = {str(x) for x in list(focus.get("priority_goal_types") or []) if str(x or "").strip()}
    priority_scores = focus.get("priority_scores") if isinstance(focus, dict) else {}
    if not isinstance(priority_scores, dict):
        priority_scores = {}
    goal_id_scores = priority_scores.get("goal_id")
    goal_type_scores = priority_scores.get("goal_type")
    if not isinstance(goal_id_scores, dict):
        goal_id_scores = {}
    if not isinstance(goal_type_scores, dict):
        goal_type_scores = {}

    def _sort_key(g: dict[str, Any]) -> tuple[int, int, int, float, float, int, str]:
        gid = str(g.get("goal_id") or "")
        goal_type = str(g.get("goal_type") or "")
        prio = int(g.get("priority") or 0)
        gid_score = float(goal_id_scores.get(gid) or 0.0)
        goal_type_score = float(goal_type_scores.get(goal_type) or 0.0)
        return (
            0 if gid in active_goal_ids else 1,
            0 if gid in priority_goal_ids else 1,
            0 if goal_type in priority_goal_types else 1,
            -float(gid_score),
            -float(goal_type_score),
            -int(prio),
            gid,
        )

    return list(sorted(list(goals or []), key=_sort_key))


def _reorder_deliberation_states(states: list[dict[str, Any]], *, focus: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Deliberation 用 states を構造情報ベースで再整列する。
    """

    # --- runtime attention_targets による state.kind 優先と、mood重複回避 ---
    priority_state_kinds = {str(x) for x in list(focus.get("priority_state_kinds") or []) if str(x or "").strip()}

    def _sort_key(s: dict[str, Any]) -> tuple[int, int, int]:
        kind = str(s.get("kind") or "")
        last_confirmed_at = int(s.get("last_confirmed_at") or 0)
        state_id = int(s.get("state_id") or 0)
        is_mood_dup = 1 if kind == "long_mood_state" else 0
        is_priority_kind = 0 if kind in priority_state_kinds else 1
        return (is_mood_dup, is_priority_kind, -int(last_confirmed_at or state_id))

    return list(sorted(list(states or []), key=_sort_key))


def _reorder_deliberation_events(
    events: list[dict[str, Any]],
    *,
    trigger_source_event_id: int | None,
    focus: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Deliberation 用 events を構造情報ベースで再整列する。
    """

    # --- トリガ起点イベントを最優先し、継続中は自発行動の結果/発話を少し優先 ---
    continuity_bias = str(focus.get("continuity_bias") or "medium")
    priority_event_sources = {str(x) for x in list(focus.get("priority_event_sources") or []) if str(x or "").strip()}
    source_rank_when_continuity = {
        "autonomy_message": 0,
        "action_result": 1,
        "vision_capture_response": 2,
    }

    def _sort_key(e: dict[str, Any]) -> tuple[int, int, int, int]:
        event_id = int(e.get("event_id") or 0)
        source = str(e.get("source") or "")
        created_at = int(e.get("created_at") or 0)
        trigger_match = 0 if (trigger_source_event_id is not None and int(event_id) == int(trigger_source_event_id)) else 1
        continuity_rank = 9
        if continuity_bias == "high":
            continuity_rank = int(source_rank_when_continuity.get(source, 9))
        focus_source_rank = 0 if source in priority_event_sources else 1
        return (trigger_match, focus_source_rank, continuity_rank, -int(created_at or event_id))

    return list(sorted(list(events or []), key=_sort_key))


def _trim_deliberation_materials(
    *,
    event_rows: list[dict[str, Any]],
    state_rows: list[dict[str, Any]],
    goals: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    focus: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    focus の最終件数に合わせて Deliberation 材料を切り詰める。
    """

    # --- final 件数は focus で決める（構造情報ベース） ---
    limits = focus.get("limits") if isinstance(focus, dict) else {}
    final_limits = limits.get("final") if isinstance(limits, dict) else {}
    if not isinstance(final_limits, dict):
        final_limits = {}

    event_limit = int(final_limits.get("events") or 24)
    state_limit = int(final_limits.get("states") or 24)
    goal_limit = int(final_limits.get("goals") or 8)
    intent_limit = int(final_limits.get("intents") or 8)

    return (
        list(event_rows or [])[:event_limit],
        list(state_rows or [])[:state_limit],
        list(goals or [])[:goal_limit],
        list(intents or [])[:intent_limit],
    )


def _update_runtime_attention_targets_from_action_result(
    *,
    intent: Intent,
    action_type: str,
    capability_name: str,
    result_status: str,
    goal_id_hint: str | None,
    now_system_ts: int,
) -> None:
    """
    ActionResult 確定を短期ランタイム状態（attention_targets）へ反映する。

    方針:
        - 自然言語本文は使わない。
        - action_type / capability / goal_id / result_status の構造情報だけ使う。
    """

    # --- 失敗でも「何に注意が向いていたか」の痕跡として残す ---
    items: list[dict[str, Any]] = []
    action_type_norm = str(action_type or "").strip()
    capability_norm = str(capability_name or "").strip()
    result_status_norm = str(result_status or "").strip()
    goal_id_norm = str(goal_id_hint or "").strip() or (str(intent.goal_id).strip() if intent.goal_id else "")

    if action_type_norm:
        items.append(
            {
                "type": "action_type",
                "value": str(action_type_norm),
                "weight": 0.95 if result_status_norm != "failed" else 0.75,
            }
        )
    if capability_norm:
        items.append(
            {
                "type": "capability",
                "value": str(capability_norm),
                "weight": 0.8 if result_status_norm != "failed" else 0.6,
            }
        )
    if goal_id_norm:
        items.append(
            {
                "type": "goal_id",
                "value": str(goal_id_norm),
                "weight": 1.0 if result_status_norm != "failed" else 0.7,
            }
        )

    # --- event source の型も短期的な連続性ヒントとして持つ ---
    items.append(
        {
            "type": "event_source",
            "value": "action_result",
            "weight": 0.4,
        }
    )

    get_runtime_blackboard().merge_attention_targets(
        items=items,
        now_system_ts=int(now_system_ts),
    )


def _maybe_create_goal_from_decision(
    *,
    db,
    decision_id: str,
    action_type: str,
    action_payload_json: str | None,
    priority: int,
    now_system_ts: int,
) -> str | None:
    """ActionDecision から goals を1件生成し、goal_id を返す。"""

    # --- action_payload を読む ---
    payload = common_utils.json_loads_maybe(str(action_payload_json or ""))
    if not isinstance(payload, dict):
        payload = {}

    # --- goal 文言が無ければ作らない ---
    goal_title = str(payload.get("goal") or "").strip()
    if not goal_title:
        return None

    # --- goal_type を action_type から決める ---
    action_type_norm = str(action_type or "").strip()
    goal_type = "research" if action_type_norm == "web_research" else "task"

    # --- Goal を1件作成（単一ユーザー前提・decision単位） ---
    goal_id = str(uuid.uuid4())
    db.add(
        Goal(
            goal_id=str(goal_id),
            title=str(goal_title)[:240],
            goal_type=str(goal_type),
            status="active",
            priority=max(0, min(100, int(priority))),
            target_condition_json=common_utils.json_dumps(
                {
                    "decision_id": str(decision_id),
                    "action_type": str(action_type_norm),
                    "action_payload": payload,
                }
            ),
            horizon="short",
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )
    return str(goal_id)


def _upsert_world_model_item_from_action_result(
    *,
    db,
    source_event_id: int,
    source_result_id: str,
    capability_name: str,
    result_status: str,
    summary_text: str,
    result_payload_json: str,
    now_system_ts: int,
    now_domain_ts: int,
) -> None:
    """ActionResult を world_model_items に集約する。"""

    # --- web_access 以外は現時点では対象外 ---
    if str(capability_name or "").strip() != "web_access":
        return

    # --- payload を読む ---
    payload = common_utils.json_loads_maybe(str(result_payload_json or ""))
    if not isinstance(payload, dict):
        payload = {}

    query = str(payload.get("query") or "").strip()
    goal = str(payload.get("goal") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    findings_raw = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    sources_raw = payload.get("sources") if isinstance(payload.get("sources"), list) else []

    # --- findings を短い文字列配列へ正規化 ---
    findings: list[str] = []
    for x in findings_raw:
        if isinstance(x, dict):
            text_v = str(x.get("text") or x.get("summary") or x.get("title") or "").strip()
        else:
            text_v = str(x or "").strip()
        if not text_v:
            continue
        findings.append(text_v[:500])
        if len(findings) >= 20:
            break

    # --- sources を監査しやすい形に正規化 ---
    sources: list[dict[str, Any]] = []
    for x in sources_raw:
        if isinstance(x, dict):
            url = str(x.get("url") or "").strip()
            title = str(x.get("title") or "").strip()
        else:
            url = str(x or "").strip()
            title = ""
        if not url and not title:
            continue
        sources.append({"url": url[:1000], "title": title[:240]})
        if len(sources) >= 10:
            break

    # --- observation_class / confidence を決める ---
    result_status_norm = str(result_status or "").strip()
    has_findings = bool(findings)
    has_sources = bool(sources)
    if result_status_norm == "success" and has_findings and has_sources:
        observation_class = "fact"
        confidence = 0.93
    elif result_status_norm in {"success", "partial"} and has_findings:
        observation_class = "fact"
        confidence = 0.84 if result_status_norm == "partial" else 0.88
    elif result_status_norm in {"success", "partial"} and has_sources:
        observation_class = "inferred"
        confidence = 0.70
    elif result_status_norm == "no_effect":
        observation_class = "unknown"
        confidence = 0.20
    else:
        observation_class = "unknown"
        confidence = 0.10

    # --- item_key（同一 query の観測を統合） ---
    query_key = str(query).strip().lower()
    if not query_key:
        query_key = hashlib.sha256(str(summary_text or "").encode("utf-8")).hexdigest()[:24]
    item_key = f"web_research:{query_key}"

    # --- 保存JSON ---
    entity_obj = {
        "kind": "web_research_query",
        "query": str(query),
        "goal": str(goal),
    }
    relation_obj = {
        "result_status": str(result_status_norm),
        "capability_name": "web_access",
    }
    location_obj = {
        "space": "web",
    }
    affordance_obj = {
        "summary": str(summary_text or "")[:1000],
        "findings": list(findings),
        "sources": list(sources),
        "notes": str(notes)[:1000],
    }

    # --- フィンガープリント（内容比較用） ---
    fingerprint_payload = {
        "summary": str(summary_text or ""),
        "findings": list(findings),
        "sources": list(sources),
        "result_status": str(result_status_norm),
    }
    content_fingerprint = hashlib.sha256(
        common_utils.json_dumps(fingerprint_payload).encode("utf-8")
    ).hexdigest()

    # --- world_model_items を item_key 単位で原子的 UPSERT ---
    db.execute(
        text(
            """
            INSERT INTO world_model_items(
                item_id,
                source_event_id,
                source_result_id,
                item_key,
                observation_class,
                entity_json,
                relation_json,
                location_json,
                affordance_json,
                content_fingerprint,
                observation_count,
                confidence,
                freshness_at,
                active,
                created_at,
                updated_at
            )
            VALUES(
                :item_id,
                :source_event_id,
                :source_result_id,
                :item_key,
                :observation_class,
                :entity_json,
                :relation_json,
                :location_json,
                :affordance_json,
                :content_fingerprint,
                1,
                :confidence,
                :freshness_at,
                1,
                :created_at,
                :updated_at
            )
            ON CONFLICT(item_key) DO UPDATE SET
                source_event_id = excluded.source_event_id,
                source_result_id = excluded.source_result_id,
                observation_class = excluded.observation_class,
                entity_json = excluded.entity_json,
                relation_json = excluded.relation_json,
                location_json = excluded.location_json,
                affordance_json = excluded.affordance_json,
                content_fingerprint = excluded.content_fingerprint,
                observation_count = world_model_items.observation_count + 1,
                confidence = CASE
                    WHEN world_model_items.content_fingerprint = excluded.content_fingerprint THEN
                        MIN(1.0, world_model_items.confidence * 0.80 + excluded.confidence * 0.20 + 0.05)
                    ELSE
                        MAX(0.0, world_model_items.confidence * 0.60 + excluded.confidence * 0.40 - 0.15)
                END,
                freshness_at = MAX(COALESCE(world_model_items.freshness_at, 0), excluded.freshness_at),
                active = excluded.active,
                updated_at = excluded.updated_at
            """
        ),
        {
            "item_id": str(uuid.uuid4()),
            "source_event_id": int(source_event_id),
            "source_result_id": str(source_result_id),
            "item_key": str(item_key),
            "observation_class": str(observation_class),
            "entity_json": common_utils.json_dumps(entity_obj),
            "relation_json": common_utils.json_dumps(relation_obj),
            "location_json": common_utils.json_dumps(location_obj),
            "affordance_json": common_utils.json_dumps(affordance_obj),
            "content_fingerprint": str(content_fingerprint),
            "confidence": float(max(0.0, min(1.0, confidence))),
            "freshness_at": int(max(0, int(now_domain_ts))),
            "created_at": int(now_system_ts),
            "updated_at": int(now_system_ts),
        },
    )


def _enqueue_standard_post_action_jobs(
    *,
    db,
    event_id: int,
    result_id: str,
    now_system_ts: int,
) -> None:
    """ActionResult 保存後に共通で投入する worker jobs を追加する。"""

    # --- recall判定ジョブを投入 ---
    db.add(
        Job(
            kind="promote_action_result_to_searchable",
            payload_json=common_utils.json_dumps({"result_id": str(result_id)}),
            status=int(_JOB_PENDING),
            run_after=int(now_system_ts),
            tries=0,
            last_error=None,
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )

    # --- 記憶更新（WritePlan）ジョブを投入 ---
    db.add(
        Job(
            kind="generate_write_plan",
            payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
            status=int(_JOB_PENDING),
            run_after=int(now_system_ts),
            tries=0,
            last_error=None,
            created_at=int(now_system_ts),
            updated_at=int(now_system_ts),
        )
    )


def _finalize_intent_and_save_action_result(
    *,
    db,
    intent: Intent,
    decision_id: str,
    action_type: str,
    capability_name: str,
    result_status: str,
    summary_text: str,
    result_payload_json: str,
    useful_for_recall_hint: int,
    now_system_ts: int,
    now_domain_ts: int,
    goal_id_hint: str | None = None,
    next_trigger: dict[str, Any] | None = None,
) -> str:
    """
    ActionResult/Event を保存し、Intent を終端化する（execute_intent/callback 共通処理）。

    Returns:
        保存した ActionResult.result_id
    """

    # --- event: action_result（既定 searchable=0） ---
    result_event = Event(
        created_at=int(now_domain_ts),
        updated_at=int(now_domain_ts),
        searchable=0,
        client_id=None,
        source="action_result",
        user_text=str(summary_text),
        assistant_text=None,
        entities_json="[]",
        client_context_json=common_utils.json_dumps(
            {
                "intent_id": str(intent.intent_id),
                "decision_id": str(decision_id),
                "action_type": str(action_type),
                "capability": str(capability_name),
                "result_status": str(result_status),
            }
        ),
    )
    db.add(result_event)
    db.flush()

    # --- action_results に保存 ---
    result_id = str(uuid.uuid4())
    db.add(
        ActionResult(
            result_id=str(result_id),
            event_id=int(result_event.event_id),
            intent_id=str(intent.intent_id),
            decision_id=str(decision_id),
            capability_name=str(capability_name),
            result_status=str(result_status),
            result_payload_json=str(result_payload_json),
            summary_text=str(summary_text),
            useful_for_recall_hint=int(useful_for_recall_hint),
            recall_decision=-1,
            recall_decided_at=None,
            created_at=int(now_system_ts),
        )
    )
    # --- world_model_items が source_result_id(FK) を参照するため、親行を先に永続化する ---
    db.flush()

    # --- world_model_items へ集約（対象外 capability は no-op） ---
    _upsert_world_model_item_from_action_result(
        db=db,
        source_event_id=int(result_event.event_id),
        source_result_id=str(result_id),
        capability_name=str(capability_name),
        result_status=str(result_status),
        summary_text=str(summary_text),
        result_payload_json=str(result_payload_json),
        now_system_ts=int(now_system_ts),
        now_domain_ts=int(now_domain_ts),
    )

    # --- agenda thread の正本を即時同期し、完了時配信の根拠を先に揃える ---
    result_payload_obj = common_utils.json_loads_maybe(str(result_payload_json or "{}"))
    if not isinstance(result_payload_obj, dict):
        raise RuntimeError("result_payload_json is not an object")
    _sync_agenda_thread_after_action_result(
        db=db,
        decision_id=str(decision_id),
        result_event_id=int(result_event.event_id),
        result_id=str(result_id),
        action_type=str(action_type),
        result_status=str(result_status),
        result_payload=dict(result_payload_obj),
        now_system_ts=int(now_system_ts),
    )

    # --- intent 状態を終端へ遷移 ---
    if str(result_status) == "failed":
        db.execute(
            text(
                """
                UPDATE intents
                   SET status='dropped',
                       dropped_reason=:dropped_reason,
                       dropped_at=:dropped_at,
                       last_result_status=:last_result_status,
                       updated_at=:updated_at
                 WHERE intent_id=:intent_id
                """
            ),
            {
                "dropped_reason": str(summary_text)[:1000],
                "dropped_at": int(now_domain_ts),
                "last_result_status": str(result_status),
                "updated_at": int(now_system_ts),
                "intent_id": str(intent.intent_id),
            },
        )
    else:
        db.execute(
            text(
                """
                UPDATE intents
                   SET status='done',
                       last_result_status=:last_result_status,
                       updated_at=:updated_at
                 WHERE intent_id=:intent_id
                """
            ),
            {
                "last_result_status": str(result_status),
                "updated_at": int(now_system_ts),
                "intent_id": str(intent.intent_id),
            },
        )

        # --- goal は成功/部分成功で完了に寄せる ---
        goal_id_for_update = goal_id_hint
        if goal_id_for_update is None and intent.goal_id is not None and str(intent.goal_id).strip():
            goal_id_for_update = str(intent.goal_id)
        if goal_id_for_update is not None and str(goal_id_for_update).strip():
            db.execute(
                text(
                    """
                    UPDATE goals
                       SET status='done',
                           updated_at=:updated_at
                     WHERE goal_id=:goal_id
                       AND status <> 'done'
                       AND status <> 'dropped'
                    """
                ),
                {
                    "updated_at": int(now_system_ts),
                    "goal_id": str(goal_id_for_update),
                },
            )

    # --- 短期ランタイム状態へ attention_targets を反映（次回 Deliberation の材料選別用） ---
    _update_runtime_attention_targets_from_action_result(
        intent=intent,
        action_type=str(action_type),
        capability_name=str(capability_name),
        result_status=str(result_status),
        goal_id_hint=goal_id_hint,
        now_system_ts=int(now_system_ts),
    )

    # --- next_trigger があれば投入 ---
    if isinstance(next_trigger, dict) and bool(next_trigger.get("required")):
        trigger_type = str(next_trigger.get("trigger_type") or "event")
        trigger_key = str(next_trigger.get("trigger_key") or f"next:{str(result_id)}")
        trigger_payload = next_trigger.get("payload") if isinstance(next_trigger.get("payload"), dict) else {}
        trigger_scheduled_at_raw = next_trigger.get("scheduled_at")
        trigger_scheduled_at = (
            int(trigger_scheduled_at_raw) if trigger_scheduled_at_raw is not None else int(now_domain_ts)
        )
        db.execute(
            text(
                """
                INSERT OR IGNORE INTO autonomy_triggers(
                    trigger_id,
                    trigger_type,
                    trigger_key,
                    source_event_id,
                    payload_json,
                    status,
                    scheduled_at,
                    claim_token,
                    claimed_at,
                    attempts,
                    last_error,
                    dropped_reason,
                    dropped_at,
                    created_at,
                    updated_at
                )
                VALUES(
                    :trigger_id,
                    :trigger_type,
                    :trigger_key,
                    :source_event_id,
                    :payload_json,
                    'queued',
                    :scheduled_at,
                    NULL,
                    NULL,
                    0,
                    NULL,
                    '',
                    NULL,
                    :created_at,
                    :updated_at
                )
                """
            ),
            {
                "trigger_id": str(uuid.uuid4()),
                "trigger_type": str(trigger_type),
                "trigger_key": str(trigger_key),
                "source_event_id": int(result_event.event_id),
                "payload_json": common_utils.json_dumps(trigger_payload),
                "scheduled_at": int(trigger_scheduled_at),
                "created_at": int(now_system_ts),
                "updated_at": int(now_system_ts),
            },
        )

    # --- 後続ジョブを投入 ---
    _enqueue_standard_post_action_jobs(
        db=db,
        event_id=int(result_event.event_id),
        result_id=str(result_id),
        now_system_ts=int(now_system_ts),
    )
    return str(result_id)


def _collect_deliberation_input(
    db,
    *,
    trigger: AutonomyTrigger,
    persona_text: str,
    addon_text: str,
    second_person_label: str,
) -> dict[str, Any]:
    """DeliberationContextPack 相当の入力を構築する。"""

    # --- trigger payload（早めに読む: focus構築にも使う） ---
    trigger_payload = common_utils.json_loads_maybe(str(trigger.payload_json or ""))
    if not isinstance(trigger_payload, dict):
        trigger_payload = {}

    # --- 人格判断の材料（構造化済みの好み/短期ランタイム状態） ---
    confirmed_preferences = _load_confirmed_preferences_snapshot_for_deliberation(db)
    current_thought_snapshot = _load_current_thought_snapshot_for_deliberation(db)
    agenda_threads_snapshot = _load_agenda_threads_snapshot_for_deliberation(db)
    runtime_blackboard_snapshot = get_runtime_blackboard().snapshot()
    if not isinstance(runtime_blackboard_snapshot, dict):
        runtime_blackboard_snapshot = {}

    # --- intents は goals優先順にも使うので先に取得しておく ---
    intent_rows = (
        db.query(Intent)
        .filter(Intent.status.in_(["queued", "running", "blocked"]))
        .order_by(Intent.updated_at.desc(), Intent.created_at.desc())
        .limit(16)
        .all()
    )
    intents = [
        {
            "intent_id": str(x.intent_id),
            "decision_id": str(x.decision_id),
            "goal_id": (str(x.goal_id) if x.goal_id is not None else None),
            "action_type": str(x.action_type),
            "status": str(x.status),
            "priority": int(x.priority),
            "scheduled_at": (int(x.scheduled_at) if x.scheduled_at is not None else None),
            "blocked_reason": (str(x.blocked_reason) if x.blocked_reason is not None else None),
        }
        for x in intent_rows
    ]
    active_goal_ids = []
    for it in list(intents or []):
        gid = str(it.get("goal_id") or "").strip()
        if gid and gid not in active_goal_ids:
            active_goal_ids.append(gid)

    # --- Deliberation 材料選別用の focus（構造情報ベース） ---
    persona_deliberation_focus = _build_persona_deliberation_focus(
        trigger=trigger,
        confirmed_preferences=confirmed_preferences,
        runtime_blackboard_snapshot=runtime_blackboard_snapshot,
        current_thought_snapshot=current_thought_snapshot,
        agenda_threads_snapshot=agenda_threads_snapshot,
        active_goal_ids=active_goal_ids,
    )
    focus_limits = persona_deliberation_focus.get("limits") if isinstance(persona_deliberation_focus, dict) else {}
    fetch_limits = focus_limits.get("fetch") if isinstance(focus_limits, dict) else {}
    if not isinstance(fetch_limits, dict):
        fetch_limits = {}
    fetch_events_limit = int(fetch_limits.get("events") or 24)
    fetch_states_limit = int(fetch_limits.get("states") or 24)
    fetch_goals_limit = int(fetch_limits.get("goals") or 8)

    # --- events（会話想起ノイズ制御: deliberation_decision は常時除外） ---
    recent_events = (
        db.query(Event)
        .filter(Event.searchable == 1)
        .filter(Event.source != "deliberation_decision")
        .order_by(Event.event_id.desc())
        .limit(fetch_events_limit)
        .all()
    )
    event_rows = [
        {
            "event_id": int(x.event_id),
            "created_at": int(x.created_at),
            "source": str(x.source),
            "user_text": str(x.user_text or "")[:800],
            "assistant_text": str(x.assistant_text or "")[:800],
        }
        for x in recent_events
    ]

    # --- state ---
    recent_states = (
        db.query(State)
        .filter(State.searchable == 1)
        .filter(State.kind != "current_thought_state")
        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
        .limit(fetch_states_limit)
        .all()
    )
    state_rows = [
        {
            "state_id": int(s.state_id),
            "kind": str(s.kind),
            "body_text": str(s.body_text)[:800],
            "payload": common_utils.json_loads_maybe(str(s.payload_json or "")),
            "confidence": float(s.confidence),
            "last_confirmed_at": int(s.last_confirmed_at),
        }
        for s in recent_states
    ]

    # --- goals ---
    goals_rows = db.execute(
        text(
            """
            SELECT goal_id, title, goal_type, status, priority, target_condition_json, horizon
             FROM goals
             WHERE status = 'active'
             ORDER BY priority DESC, updated_at DESC
             LIMIT :row_limit
            """
        ),
        {"row_limit": int(fetch_goals_limit)},
    ).fetchall()
    goals = [
        {
            "goal_id": str(r[0]),
            "title": str(r[1]),
            "goal_type": str(r[2]),
            "status": str(r[3]),
            "priority": int(r[4] or 0),
            "target_condition": common_utils.json_loads_maybe(str(r[5] or "{}")),
            "horizon": str(r[6] or ""),
        }
        for r in goals_rows
    ]
    # --- 材料を構造情報ベースで再整列して最終件数へ絞る ---
    event_rows = _reorder_deliberation_events(
        event_rows,
        trigger_source_event_id=(int(trigger.source_event_id) if trigger.source_event_id is not None else None),
        focus=persona_deliberation_focus,
    )
    state_rows = _reorder_deliberation_states(state_rows, focus=persona_deliberation_focus)
    goals = _reorder_deliberation_goals(goals, focus=persona_deliberation_focus)
    intents = _reorder_deliberation_intents(intents, focus=persona_deliberation_focus)
    event_rows, state_rows, goals, intents = _trim_deliberation_materials(
        event_rows=event_rows,
        state_rows=state_rows,
        goals=goals,
        intents=intents,
        focus=persona_deliberation_focus,
    )

    # --- Deliberation入力には内部追跡用UUIDを出さない（evidence IDsとの混同を防ぐ） ---
    pt = str(persona_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    at = str(addon_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    sp = str(second_person_label or "").strip() or "あなた"
    return {
        "trigger": {
            "trigger_type": str(trigger.trigger_type),
            "trigger_key": str(trigger.trigger_key),
            "source_event_id": (int(trigger.source_event_id) if trigger.source_event_id is not None else None),
            "payload": trigger_payload,
            "scheduled_at": (int(trigger.scheduled_at) if trigger.scheduled_at is not None else None),
        },
        "persona": {
            "second_person_label": str(sp),
            "persona_text": str(pt),
            "addon_text": str(at),
        },
        "events": event_rows,
        "states": state_rows,
        "goals": goals,
        "intents": intents,
        "confirmed_preferences": confirmed_preferences,
        "current_thought": current_thought_snapshot,
        "agenda_threads": agenda_threads_snapshot,
        "runtime_blackboard": runtime_blackboard_snapshot,
        "persona_deliberation_focus": persona_deliberation_focus,
        "capabilities": [
            {
                "capability": "vision_perception",
                "action_types": ["observe_screen", "observe_camera"],
            },
            {
                "capability": "web_access",
                "action_types": ["web_research"],
            },
            {
                "capability": "schedule_alarm",
                "action_types": ["schedule_action"],
            },
            {
                "capability": "device_control",
                "action_types": ["device_action"],
            },
            {
                "capability": "mobility_move",
                "action_types": ["move_to"],
            },
            {
                "capability": "agent_delegate",
                "action_types": ["agent_delegate"],
                "backends": ["cli_agent"],
            },
        ],
    }


def _mark_trigger_done(
    *,
    db,
    trigger_id: str,
    claim_token: str,
    now_system_ts: int,
) -> None:
    """claimed trigger を done へ遷移する。"""
    db.execute(
        text(
            """
            UPDATE autonomy_triggers
               SET status='done',
                   claim_token=NULL,
                   claimed_at=NULL,
                   updated_at=:updated_at
             WHERE trigger_id=:trigger_id
               AND status='claimed'
               AND claim_token=:claim_token
            """
        ),
        {
            "updated_at": int(now_system_ts),
            "trigger_id": str(trigger_id),
            "claim_token": str(claim_token),
        },
    )


def _mark_trigger_dropped(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    trigger_id: str,
    claim_token: str,
    now_system_ts: int,
    reason: str,
) -> None:
    """claimed trigger を dropped へ遷移する。"""
    dropped_reason = str(reason or "").strip()
    if not dropped_reason:
        dropped_reason = "unknown_error"
    if len(dropped_reason) > 1000:
        dropped_reason = dropped_reason[:1000]
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.execute(
            text(
                """
                UPDATE autonomy_triggers
                   SET status='dropped',
                       dropped_reason=:dropped_reason,
                       dropped_at=:dropped_at,
                       last_error=:last_error,
                       updated_at=:updated_at
                 WHERE trigger_id=:trigger_id
                   AND status='claimed'
                   AND claim_token=:claim_token
                """
            ),
            {
                "dropped_reason": str(dropped_reason),
                "dropped_at": int(now_system_ts),
                "last_error": str(dropped_reason),
                "updated_at": int(now_system_ts),
                "trigger_id": str(trigger_id),
                "claim_token": str(claim_token),
            },
        )


def _handle_deliberate_once(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """Trigger から ActionDecision を作成し、必要なら Intent を生成する。"""

    # --- payload を検証 ---
    trigger_id = str(payload.get("trigger_id") or "").strip()
    claim_token = str(payload.get("claim_token") or "").strip()
    if not trigger_id or not claim_token:
        raise RuntimeError("payload.trigger_id and payload.claim_token are required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()

    try:
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- claimed trigger を取得 ---
            trigger = (
                db.query(AutonomyTrigger)
                .filter(AutonomyTrigger.trigger_id == str(trigger_id))
                .filter(AutonomyTrigger.status == "claimed")
                .filter(AutonomyTrigger.claim_token == str(claim_token))
                .one_or_none()
            )
            if trigger is None:
                return

            # --- defer_until前の早着トリガは再スケジュール ---
            if trigger.scheduled_at is not None and int(trigger.scheduled_at) > int(now_domain_ts):
                db.execute(
                    text(
                        """
                        UPDATE autonomy_triggers
                           SET status='queued',
                               claim_token=NULL,
                               claimed_at=NULL,
                               updated_at=:updated_at
                         WHERE trigger_id=:trigger_id
                           AND status='claimed'
                           AND claim_token=:claim_token
                        """
                    ),
                    {
                        "updated_at": int(now_system_ts),
                        "trigger_id": str(trigger_id),
                        "claim_token": str(claim_token),
                    },
                )
                return

            # --- Deliberation入力 ---
            cfg = get_config_store().config
            deliberation_input = _collect_deliberation_input(
                db,
                trigger=trigger,
                persona_text=str(cfg.persona_text),
                addon_text=str(cfg.addon_text),
                second_person_label=str(cfg.second_person_label),
            )
            system_prompt = prompt_builders.autonomy_deliberation_system_prompt(
                second_person_label=str(cfg.second_person_label),
            )

            # --- LLMで意思決定 ---
            resp = llm_client.generate_json_response(
                system_prompt=system_prompt,
                input_text=common_utils.json_dumps(deliberation_input),
                purpose=LlmRequestPurpose.ASYNC_AUTONOMY_DELIBERATION,
                max_tokens=int(cfg.max_tokens),
            )
            # --- LLM出力不正（JSON崩れ/契約違反）は業務エラーとして drop 扱いにする ---
            try:
                decision_raw = llm_client.response_json(resp)
            except ValueError as exc:
                raise _DeliberationInvalidOutputError(
                    drop_reason="deliberation_invalid_json",
                    detail=str(exc),
                ) from exc
            if not isinstance(decision_raw, dict):
                raise _DeliberationInvalidOutputError(
                    drop_reason="deliberation_invalid_json",
                    detail="deliberation output is not a JSON object",
                )
            try:
                decision = parse_action_decision(decision_raw)
            except ValueError as exc:
                raise _DeliberationInvalidOutputError(
                    drop_reason="deliberation_invalid_contract",
                    detail=str(exc),
                ) from exc

            # --- 実行対象の agenda thread を決めておく（後続 ActionResult の正本更新に使う） ---
            decision_agenda_thread_id = _select_agenda_thread_id_for_new_decision(
                trigger=trigger,
                deliberation_input=dict(deliberation_input or {}),
                decision=decision,
            )

            # --- event: deliberation_decision（常時 searchable=0） ---
            decision_event = Event(
                created_at=int(now_domain_ts),
                updated_at=int(now_domain_ts),
                searchable=0,
                client_id=None,
                source="deliberation_decision",
                user_text=str(decision.reason_text),
                assistant_text=None,
                entities_json="[]",
                client_context_json=common_utils.json_dumps(
                    {
                        "trigger_id": str(trigger_id),
                        "trigger_type": str(trigger.trigger_type),
                        "trigger_key": str(trigger.trigger_key),
                        "decision": decision_raw,
                    }
                ),
            )
            db.add(decision_event)
            db.flush()

            # --- ActionDecision を保存 ---
            decision_id = str(uuid.uuid4())
            db.add(
                ActionDecision(
                    decision_id=str(decision_id),
                    event_id=int(decision_event.event_id),
                    trigger_type=str(trigger.trigger_type),
                    trigger_ref=str(trigger.trigger_id),
                    agenda_thread_id=(str(decision_agenda_thread_id) if decision_agenda_thread_id else None),
                    decision_outcome=str(decision.decision_outcome),
                    action_type=(str(decision.action_type) if decision.action_type else None),
                    action_payload_json=(str(decision.action_payload_json) if decision.action_payload_json else None),
                    reason_text=str(decision.reason_text),
                    defer_reason=(str(decision.defer_reason) if decision.defer_reason else None),
                    defer_until=(int(decision.defer_until) if decision.defer_until is not None else None),
                    next_deliberation_at=(
                        int(decision.next_deliberation_at) if decision.next_deliberation_at is not None else None
                    ),
                    persona_influence_json=str(decision.persona_influence_json),
                    mood_influence_json=str(decision.mood_influence_json),
                    console_delivery_json=str(decision.console_delivery_json),
                    evidence_event_ids_json=str(decision.evidence_event_ids_json),
                    evidence_state_ids_json=str(decision.evidence_state_ids_json),
                    evidence_goal_ids_json=str(decision.evidence_goal_ids_json),
                    confidence=float(decision.confidence),
                    created_at=int(now_system_ts),
                )
            )

            # --- 記憶更新（WritePlan）ジョブを投入（decision は searchable=0 でも対象） ---
            db.add(
                Job(
                    kind="generate_write_plan",
                    payload_json=common_utils.json_dumps({"event_id": int(decision_event.event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_system_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_system_ts),
                    updated_at=int(now_system_ts),
                )
            )

            # --- decision_outcome=do_action: Intent を冪等生成して execute_intent を投入 ---
            if str(decision.decision_outcome) == "do_action":
                goal_id_for_intent = _maybe_create_goal_from_decision(
                    db=db,
                    decision_id=str(decision_id),
                    action_type=str(decision.action_type or ""),
                    action_payload_json=(str(decision.action_payload_json) if decision.action_payload_json else None),
                    priority=int(decision.priority),
                    now_system_ts=int(now_system_ts),
                )

                # --- 親行（action_decisions / goals）を先に flush する ---
                # `intents` は `decision_id` / `goal_id` の外部キーを持つ。
                # ここで生SQL `INSERT` を使うため、ORM の pending 行を明示 flush して
                # FK親を先にDBへ反映してから `intents` を追加する。
                db.flush()

                proposed_intent_id = str(uuid.uuid4())
                db.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO intents(
                            intent_id,
                            decision_id,
                            goal_id,
                            action_type,
                            action_payload_json,
                            status,
                            priority,
                            scheduled_at,
                            blocked_reason,
                            dropped_reason,
                            dropped_at,
                            last_result_status,
                            created_at,
                            updated_at
                        )
                        VALUES(
                            :intent_id,
                            :decision_id,
                            :goal_id,
                            :action_type,
                            :action_payload_json,
                            'queued',
                            :priority,
                            NULL,
                            NULL,
                            '',
                            NULL,
                            NULL,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    {
                        "intent_id": str(proposed_intent_id),
                        "decision_id": str(decision_id),
                        "goal_id": (str(goal_id_for_intent) if goal_id_for_intent else None),
                        "action_type": str(decision.action_type or ""),
                        "action_payload_json": str(decision.action_payload_json or "{}"),
                        "priority": int(decision.priority),
                        "created_at": int(now_system_ts),
                        "updated_at": int(now_system_ts),
                    },
                )
                intent_row = db.execute(
                    text("SELECT intent_id FROM intents WHERE decision_id=:decision_id LIMIT 1"),
                    {"decision_id": str(decision_id)},
                ).fetchone()
                if intent_row is not None and str(intent_row[0] or "").strip():
                    db.add(
                        Job(
                            kind="execute_intent",
                            payload_json=common_utils.json_dumps({"intent_id": str(intent_row[0])}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_system_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_system_ts),
                            updated_at=int(now_system_ts),
                        )
                    )

            # --- defer: 再検討トリガを投入 ---
            if str(decision.decision_outcome) == "defer":
                defer_at = int(decision.next_deliberation_at or now_domain_ts)
                db.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO autonomy_triggers(
                            trigger_id,
                            trigger_type,
                            trigger_key,
                            source_event_id,
                            payload_json,
                            status,
                            scheduled_at,
                            claim_token,
                            claimed_at,
                            attempts,
                            last_error,
                            dropped_reason,
                            dropped_at,
                            created_at,
                            updated_at
                        )
                        VALUES(
                            :trigger_id,
                            'heartbeat',
                            :trigger_key,
                            NULL,
                            :payload_json,
                            'queued',
                            :scheduled_at,
                            NULL,
                            NULL,
                            0,
                            NULL,
                            '',
                            NULL,
                            :created_at,
                            :updated_at
                        )
                        """
                    ),
                    {
                        "trigger_id": str(uuid.uuid4()),
                        "trigger_key": f"defer:{str(decision_id)}:{int(defer_at)}",
                        "payload_json": common_utils.json_dumps(
                            {
                                "source": "defer",
                                "decision_id": str(decision_id),
                                "defer_reason": str(decision.defer_reason or ""),
                            }
                        ),
                        "scheduled_at": int(defer_at),
                        "created_at": int(now_system_ts),
                        "updated_at": int(now_system_ts),
                    },
                )

            # --- trigger を done へ遷移 ---
            _mark_trigger_done(
                db=db,
                trigger_id=str(trigger_id),
                claim_token=str(claim_token),
                now_system_ts=int(now_system_ts),
            )
    except _DeliberationInvalidOutputError as exc:
        # --- 想定内のLLM出力不正は warning に落として trigger を drop する ---
        # --- ログ上で JSON系の異常を見分けやすくするため、分類ラベルを付ける ---
        anomaly_label = "JSON異常(形式不整合)"
        if str(exc.drop_reason) == "deliberation_invalid_json":
            anomaly_label = "JSON異常(パース)"
        logger.warning(
            "deliberate_once dropped invalid LLM output [%s] trigger_id=%s reason=%s detail=%s",
            str(anomaly_label),
            str(trigger_id),
            str(exc.drop_reason),
            str(exc.detail),
        )
        _mark_trigger_dropped(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            trigger_id=str(trigger_id),
            claim_token=str(claim_token),
            now_system_ts=int(now_system_ts),
            reason=str(exc.drop_reason),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("deliberate_once failed trigger_id=%s", str(trigger_id))
        _mark_trigger_dropped(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            trigger_id=str(trigger_id),
            claim_token=str(claim_token),
            now_system_ts=int(now_system_ts),
            reason=str(exc),
        )


def _handle_execute_intent(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """Intent を実行して ActionResult を保存する。"""

    # --- payload を検証 ---
    intent_id = str(payload.get("intent_id") or "").strip()
    if not intent_id:
        raise RuntimeError("payload.intent_id is required")

    cfg = get_config_store().config
    # --- claim後に必要な情報を保持（Capability実行はDB外で行う） ---
    claimed_action_type = ""
    claimed_decision_id = ""
    claimed_goal_id: str | None = None
    claimed_console_delivery_json = ""
    intent_payload: dict[str, Any] = {}
    action_result_id_for_publish: str | None = None
    action_result_activity_state_override: str | None = None
    agent_job_activity_to_publish: dict[str, Any] | None = None

    try:
        # --- Phase 1: queued -> running claim と読み取り（短いDBトランザクション） ---
        now_system_ts = _now_utc_ts()
        now_domain_ts = _now_domain_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- queued -> running を原子的に claim ---
            claimed = db.execute(
                text(
                    """
                    UPDATE intents
                       SET status='running',
                           updated_at=:updated_at
                     WHERE intent_id=:intent_id
                       AND status='queued'
                       AND (
                            SELECT COUNT(*)
                              FROM intents AS i2
                             WHERE i2.status='running'
                               AND i2.action_type <> 'agent_delegate'
                       ) < :max_parallel
                    """
                ),
                {
                    "updated_at": int(now_system_ts),
                    "intent_id": str(intent_id),
                    "max_parallel": max(1, int(cfg.autonomy_max_parallel_intents)),
                },
            )
            if int(claimed.rowcount or 0) != 1:
                return

            # --- claim後の intent/decision を読む ---
            intent = db.query(Intent).filter(Intent.intent_id == str(intent_id)).one_or_none()
            if intent is None:
                return
            decision = db.query(ActionDecision).filter(ActionDecision.decision_id == str(intent.decision_id)).one_or_none()
            if decision is None:
                # decision が無ければ実行不能として dropped
                db.execute(
                    text(
                        """
                        UPDATE intents
                           SET status='dropped',
                               dropped_reason=:dropped_reason,
                               dropped_at=:dropped_at,
                               updated_at=:updated_at
                         WHERE intent_id=:intent_id
                        """
                    ),
                    {
                        "dropped_reason": "decision_not_found",
                        "dropped_at": int(now_domain_ts),
                        "updated_at": int(now_system_ts),
                        "intent_id": str(intent_id),
                    },
                )
                return

            # --- Capability実行に必要な値だけ保持してDBを閉じる ---
            claimed_action_type = str(intent.action_type or "")
            claimed_decision_id = str(decision.decision_id)
            claimed_goal_id = (str(intent.goal_id) if intent.goal_id is not None and str(intent.goal_id).strip() else None)
            claimed_console_delivery_json = str(decision.console_delivery_json or "")
            intent_payload_raw = common_utils.json_loads_maybe(str(intent.action_payload_json or ""))
            intent_payload = dict(intent_payload_raw) if isinstance(intent_payload_raw, dict) else {}

        # --- Phase 2: capability 実行（DBロックを持たない） ---
        is_agent_delegate = False
        if str(claimed_action_type) == "web_research":
            result = execute_web_research(
                llm_client=llm_client,
                second_person_label=str(cfg.second_person_label),
                query=str(intent_payload.get("query") or ""),
                goal=str(intent_payload.get("goal") or ""),
                constraints=[str(x) for x in list(intent_payload.get("constraints") or [])],
            )
            capability_name = "web_access"
        elif str(claimed_action_type) in {"observe_screen", "observe_camera"}:
            result = execute_vision_perception(
                llm_client=llm_client,
                action_type=str(claimed_action_type),
                action_payload=intent_payload,
            )
            capability_name = "vision_perception"
        elif str(claimed_action_type) == "schedule_action":
            result = execute_schedule_alarm(
                action_payload=intent_payload,
            )
            capability_name = "schedule_alarm"
        elif str(claimed_action_type) == "device_action":
            result = execute_device_control(
                action_payload=intent_payload,
            )
            capability_name = "device_control"
        elif str(claimed_action_type) == "move_to":
            result = execute_mobility_move(
                action_payload=intent_payload,
            )
            capability_name = "mobility_move"
        elif str(claimed_action_type) == "agent_delegate":
            # --- 汎用委譲は callback 完了時に ActionResult を保存するため、ここでは job 作成だけ行う ---
            backend = str(intent_payload.get("backend") or "").strip()
            task_instruction = str(intent_payload.get("task_instruction") or "").strip()
            if not backend:
                raise RuntimeError("agent_delegate requires action_payload.backend")
            if not task_instruction:
                raise RuntimeError("agent_delegate requires action_payload.task_instruction")
            capability_name = "agent_delegate"
            is_agent_delegate = True
            result = None
        else:
            capability_name = "unknown"
            result = CapabilityExecutionResult(
                result_status="failed",
                summary=f"unsupported action_type: {str(claimed_action_type)}",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": str(claimed_action_type),
                        "action_payload": intent_payload,
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )

        # --- Phase 3: 結果保存（別トランザクション） ---
        now_system_ts = _now_utc_ts()
        now_domain_ts = _now_domain_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- running 以外に遷移済みなら二重保存しない ---
            intent = db.query(Intent).filter(Intent.intent_id == str(intent_id)).one_or_none()
            if intent is None:
                return
            if str(intent.status) != "running":
                return

            if bool(is_agent_delegate):
                # --- agent_delegate は別プロセス runner 向け job を作成し、Intent は running のまま維持 ---
                existing_agent_job = db.query(AgentJob).filter(AgentJob.intent_id == str(intent_id)).one_or_none()
                if existing_agent_job is None:
                    new_agent_job_id = str(uuid.uuid4())
                    db.add(
                        AgentJob(
                            job_id=str(new_agent_job_id),
                            intent_id=str(intent_id),
                            decision_id=str(claimed_decision_id),
                            backend=str(intent_payload.get("backend") or ""),
                            task_instruction=str(intent_payload.get("task_instruction") or ""),
                            status="queued",
                            claim_token=None,
                            runner_id=None,
                            attempts=0,
                            heartbeat_at=None,
                            result_status=None,
                            result_summary_text=None,
                            result_details_json="{}",
                            error_code=None,
                            error_message=None,
                            created_at=int(now_system_ts),
                            started_at=None,
                            finished_at=None,
                            updated_at=int(now_system_ts),
                        )
                    )
                    db.flush()

                    # --- on_progress が silent 以外なら queued activity を出す ---
                    console_delivery_obj = parse_console_delivery(
                        common_utils.json_loads_maybe(str(claimed_console_delivery_json or ""))
                    )
                    if str(console_delivery_obj.get("on_progress") or "") != "silent":
                        agent_job_activity_to_publish = {
                            "event_id": 0,
                            "phase": "agent_job",
                            "state": "queued",
                            "action_type": "agent_delegate",
                            "capability": "agent_delegate",
                            "backend": str(intent_payload.get("backend") or ""),
                            "result_status": None,
                            "summary_text": str(intent_payload.get("task_instruction") or ""),
                            "decision_id": str(claimed_decision_id),
                            "intent_id": str(intent_id),
                            "result_id": None,
                            "agent_job_id": str(new_agent_job_id),
                            "goal_id": (str(claimed_goal_id) if claimed_goal_id is not None else None),
                        }

                # --- DBコミット後に publish するため、with を抜けてから return する ---
                pass
            else:
                if not isinstance(result, CapabilityExecutionResult):
                    raise RuntimeError("execute_intent internal error: capability result missing")

                action_result_id_for_publish = _finalize_intent_and_save_action_result(
                    db=db,
                    intent=intent,
                    decision_id=str(claimed_decision_id),
                    action_type=str(claimed_action_type),
                    capability_name=str(capability_name),
                    result_status=str(result.result_status),
                    summary_text=str(result.summary),
                    result_payload_json=str(result.result_payload_json),
                    useful_for_recall_hint=int(result.useful_for_recall_hint),
                    now_system_ts=int(now_system_ts),
                    now_domain_ts=int(now_domain_ts),
                    goal_id_hint=(str(claimed_goal_id) if claimed_goal_id is not None else None),
                    next_trigger=result.next_trigger,
                )
                action_result_activity_state_override = None

        # --- Phase 4: Console向けイベント配信（DBコミット後） ---
        if isinstance(agent_job_activity_to_publish, dict):
            try:
                _publish_autonomy_activity(
                    event_id=int(agent_job_activity_to_publish.get("event_id") or 0),
                    phase=str(agent_job_activity_to_publish.get("phase") or "agent_job"),
                    state=str(agent_job_activity_to_publish.get("state") or "queued"),
                    action_type=(str(agent_job_activity_to_publish.get("action_type")) if agent_job_activity_to_publish.get("action_type") else None),
                    capability=(str(agent_job_activity_to_publish.get("capability")) if agent_job_activity_to_publish.get("capability") else None),
                    backend=(str(agent_job_activity_to_publish.get("backend")) if agent_job_activity_to_publish.get("backend") else None),
                    result_status=(str(agent_job_activity_to_publish.get("result_status")) if agent_job_activity_to_publish.get("result_status") else None),
                    summary_text=(str(agent_job_activity_to_publish.get("summary_text")) if agent_job_activity_to_publish.get("summary_text") else None),
                    decision_id=(str(agent_job_activity_to_publish.get("decision_id")) if agent_job_activity_to_publish.get("decision_id") else None),
                    intent_id=(str(agent_job_activity_to_publish.get("intent_id")) if agent_job_activity_to_publish.get("intent_id") else None),
                    result_id=(str(agent_job_activity_to_publish.get("result_id")) if agent_job_activity_to_publish.get("result_id") else None),
                    agent_job_id=(str(agent_job_activity_to_publish.get("agent_job_id")) if agent_job_activity_to_publish.get("agent_job_id") else None),
                    goal_id=(str(agent_job_activity_to_publish.get("goal_id")) if agent_job_activity_to_publish.get("goal_id") else None),
                )
            except Exception:  # noqa: BLE001
                logger.exception("autonomy activity publish failed intent_id=%s", str(intent_id))
            return

        if action_result_id_for_publish is not None and str(action_result_id_for_publish).strip():
            try:
                _emit_autonomy_console_events_for_action_result(
                    embedding_preset_id=str(embedding_preset_id),
                    embedding_dimension=int(embedding_dimension),
                    result_id=str(action_result_id_for_publish),
                    activity_state_override=action_result_activity_state_override,
                )
            except Exception:  # noqa: BLE001
                logger.exception("autonomy console publish failed result_id=%s", str(action_result_id_for_publish))
    except Exception as exc:  # noqa: BLE001
        # --- 途中失敗時は running intent の取り残しを防ぐ ---
        fail_reason = str(exc or "").strip() or "execute_intent_failed"
        if len(fail_reason) > 1000:
            fail_reason = fail_reason[:1000]
        try:
            fail_system_ts = _now_utc_ts()
            fail_domain_ts = _now_domain_utc_ts()
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                db.execute(
                    text(
                        """
                        UPDATE intents
                           SET status='dropped',
                               dropped_reason=:dropped_reason,
                               dropped_at=:dropped_at,
                               updated_at=:updated_at
                         WHERE intent_id=:intent_id
                           AND status='running'
                        """
                    ),
                    {
                        "dropped_reason": str(fail_reason),
                        "dropped_at": int(fail_domain_ts),
                        "updated_at": int(fail_system_ts),
                        "intent_id": str(intent_id),
                    },
                )
        except Exception:  # noqa: BLE001
            logger.exception("execute_intent cleanup failed intent_id=%s", str(intent_id))
        raise

def complete_agent_job_from_runner(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    job_id: str,
    claim_token: str,
    runner_id: str,
    result_status: str,
    summary_text: str,
    details_json_obj: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    agent_runner の complete callback を処理し、ActionResult/Event/Intent を終端化する。
    """

    # --- 入力を正規化 ---
    job_id_norm = str(job_id or "").strip()
    token_norm = str(claim_token or "").strip()
    runner_id_norm = str(runner_id or "").strip()
    result_status_norm = str(result_status or "").strip()
    summary_norm = str(summary_text or "").strip()
    details_obj = dict(details_json_obj or {})
    if not job_id_norm or not token_norm or not runner_id_norm:
        raise RuntimeError("job_id / claim_token / runner_id are required")
    if result_status_norm not in {"success", "partial", "no_effect"}:
        raise RuntimeError("result_status must be success/partial/no_effect for complete")
    if not summary_norm:
        summary_norm = "agent job completed"

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    result_id_for_publish: str | None = None
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- callback 対象 job を検証 ---
        job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id_norm)).one_or_none()
        if job is None:
            raise RuntimeError("agent_job not found")
        if str(job.claim_token or "") != str(token_norm):
            raise RuntimeError("agent_job claim_token mismatch")
        if str(job.runner_id or "") != str(runner_id_norm):
            raise RuntimeError("agent_job runner_id mismatch")
        if str(job.status) in {"completed", "failed", "cancelled", "timed_out"}:
            return {
                "job_id": str(job.job_id),
                "status": str(job.status),
                "intent_id": str(job.intent_id),
                "backend": str(job.backend),
            }
        if str(job.status) not in {"claimed", "running"}:
            raise RuntimeError(f"agent_job status is not claimable for completion: {str(job.status)}")

        # --- 対応 intent を検証（running のまま維持されている前提） ---
        intent = db.query(Intent).filter(Intent.intent_id == str(job.intent_id)).one_or_none()
        if intent is None:
            raise RuntimeError("intent not found for agent_job")
        if str(intent.status) != "running":
            raise RuntimeError(f"intent status is not running: {str(intent.status)}")

        # --- job を completed に更新 ---
        job.status = "completed"
        job.result_status = str(result_status_norm)
        job.result_summary_text = str(summary_norm)
        job.result_details_json = common_utils.json_dumps(details_obj)
        job.error_code = None
        job.error_message = None
        job.started_at = int(job.started_at or now_system_ts)
        job.finished_at = int(now_system_ts)
        job.heartbeat_at = int(now_system_ts)
        job.updated_at = int(now_system_ts)

        # --- result payload は agent job 文脈付きで保存する ---
        result_payload_obj = {
            "agent_job_id": str(job.job_id),
            "backend": str(job.backend),
            "task_instruction": str(job.task_instruction),
            "details": details_obj,
        }
        useful_hint = 1 if str(result_status_norm) in {"success", "partial"} and bool(summary_norm) else 0

        result_id_for_publish = _finalize_intent_and_save_action_result(
            db=db,
            intent=intent,
            decision_id=str(job.decision_id),
            action_type="agent_delegate",
            capability_name="agent_delegate",
            result_status=str(result_status_norm),
            summary_text=str(summary_norm),
            result_payload_json=common_utils.json_dumps(result_payload_obj),
            useful_for_recall_hint=int(useful_hint),
            now_system_ts=int(now_system_ts),
            now_domain_ts=int(now_domain_ts),
            goal_id_hint=None,
            next_trigger=None,
        )
        out = {
            "job_id": str(job.job_id),
            "status": str(job.status),
            "intent_id": str(job.intent_id),
            "backend": str(job.backend),
        }
    # --- DBコミット後に Console 向けイベントを配信する ---
    if result_id_for_publish is not None and str(result_id_for_publish).strip():
        try:
            _emit_autonomy_console_events_for_action_result(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                result_id=str(result_id_for_publish),
                activity_state_override="completed",
            )
        except Exception:  # noqa: BLE001
            logger.exception("autonomy console publish failed result_id=%s", str(result_id_for_publish))
    # --- agent_job 完了状態は監視向けに常に通知する ---
    try:
        _publish_autonomy_activity(
            event_id=0,
            phase="agent_job",
            state="completed",
            action_type="agent_delegate",
            capability="agent_delegate",
            backend=str(out.get("backend") or ""),
            result_status=str(result_status_norm),
            summary_text=str(summary_norm),
            decision_id=None,
            intent_id=str(out.get("intent_id") or ""),
            result_id=(str(result_id_for_publish) if result_id_for_publish else None),
            agent_job_id=str(out.get("job_id") or ""),
            goal_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("autonomy activity publish failed agent_job_id=%s", str(out.get("job_id") or ""))
    return out


def fail_agent_job_from_runner(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    job_id: str,
    claim_token: str,
    runner_id: str,
    error_code: str | None,
    error_message: str,
) -> dict[str, Any]:
    """
    agent_runner の fail callback を処理し、ActionResult/Event/Intent を失敗終端化する。
    """

    # --- 入力を正規化 ---
    job_id_norm = str(job_id or "").strip()
    token_norm = str(claim_token or "").strip()
    runner_id_norm = str(runner_id or "").strip()
    error_code_norm = str(error_code or "").strip() or "agent_job_failed"
    error_message_norm = str(error_message or "").strip() or "agent job failed"
    if len(error_message_norm) > 1000:
        error_message_norm = error_message_norm[:1000]
    if not job_id_norm or not token_norm or not runner_id_norm:
        raise RuntimeError("job_id / claim_token / runner_id are required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    result_id_for_publish: str | None = None
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- callback 対象 job を検証 ---
        job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id_norm)).one_or_none()
        if job is None:
            raise RuntimeError("agent_job not found")
        if str(job.claim_token or "") != str(token_norm):
            raise RuntimeError("agent_job claim_token mismatch")
        if str(job.runner_id or "") != str(runner_id_norm):
            raise RuntimeError("agent_job runner_id mismatch")
        if str(job.status) in {"completed", "failed", "cancelled", "timed_out"}:
            return {
                "job_id": str(job.job_id),
                "status": str(job.status),
                "intent_id": str(job.intent_id),
                "backend": str(job.backend),
            }
        if str(job.status) not in {"claimed", "running"}:
            raise RuntimeError(f"agent_job status is not claimable for failure: {str(job.status)}")

        # --- 対応 intent を検証 ---
        intent = db.query(Intent).filter(Intent.intent_id == str(job.intent_id)).one_or_none()
        if intent is None:
            raise RuntimeError("intent not found for agent_job")
        if str(intent.status) != "running":
            raise RuntimeError(f"intent status is not running: {str(intent.status)}")

        # --- job を failed に更新 ---
        job.status = "failed"
        job.result_status = "failed"
        job.result_summary_text = str(error_message_norm)
        job.result_details_json = common_utils.json_dumps(
            {
                "agent_job_id": str(job.job_id),
                "backend": str(job.backend),
                "error_code": str(error_code_norm),
                "error_message": str(error_message_norm),
            }
        )
        job.error_code = str(error_code_norm)
        job.error_message = str(error_message_norm)
        job.started_at = int(job.started_at or now_system_ts)
        job.finished_at = int(now_system_ts)
        job.heartbeat_at = int(now_system_ts)
        job.updated_at = int(now_system_ts)

        result_id_for_publish = _finalize_intent_and_save_action_result(
            db=db,
            intent=intent,
            decision_id=str(job.decision_id),
            action_type="agent_delegate",
            capability_name="agent_delegate",
            result_status="failed",
            summary_text=str(error_message_norm),
            result_payload_json=str(job.result_details_json),
            useful_for_recall_hint=0,
            now_system_ts=int(now_system_ts),
            now_domain_ts=int(now_domain_ts),
            goal_id_hint=None,
            next_trigger=None,
        )
        out = {
            "job_id": str(job.job_id),
            "status": str(job.status),
            "intent_id": str(job.intent_id),
            "backend": str(job.backend),
        }
    # --- DBコミット後に Console 向けイベントを配信する ---
    if result_id_for_publish is not None and str(result_id_for_publish).strip():
        try:
            _emit_autonomy_console_events_for_action_result(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                result_id=str(result_id_for_publish),
                activity_state_override="failed",
            )
        except Exception:  # noqa: BLE001
            logger.exception("autonomy console publish failed result_id=%s", str(result_id_for_publish))
    # --- agent_job 失敗状態は監視向けに常に通知する ---
    try:
        _publish_autonomy_activity(
            event_id=0,
            phase="agent_job",
            state="failed",
            action_type="agent_delegate",
            capability="agent_delegate",
            backend=str(out.get("backend") or ""),
            result_status="failed",
            summary_text=str(error_message_norm),
            decision_id=None,
            intent_id=str(out.get("intent_id") or ""),
            result_id=(str(result_id_for_publish) if result_id_for_publish else None),
            agent_job_id=str(out.get("job_id") or ""),
            goal_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("autonomy activity publish failed agent_job_id=%s", str(out.get("job_id") or ""))
    return out


def _handle_promote_action_result_to_searchable(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    payload: dict[str, Any],
) -> None:
    """ActionResult の recall 可否を最終確定する。"""

    # --- payload を検証 ---
    result_id = str(payload.get("result_id") or "").strip()
    if not result_id:
        raise RuntimeError("payload.result_id is required")

    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        result = db.query(ActionResult).filter(ActionResult.result_id == str(result_id)).one_or_none()
        if result is None:
            return
        if int(result.recall_decision) != -1:
            return

        # --- 最終判定（唯一責務） ---
        summary = str(result.summary_text or "").strip()
        should_promote = (
            int(result.useful_for_recall_hint) == 1
            and str(result.result_status) in {"success", "partial"}
            and bool(summary)
        )
        decision = 1 if bool(should_promote) else 0
        result.recall_decision = int(decision)
        result.recall_decided_at = int(now_system_ts)

        # --- 採用時だけ events.searchable=1 へ昇格 ---
        if int(decision) == 1:
            db.execute(
                text(
                    """
                    UPDATE events
                       SET searchable=1,
                           updated_at=:updated_at
                     WHERE event_id=:event_id
                    """
                ),
                {
                    "updated_at": int(now_domain_ts),
                    "event_id": int(result.event_id),
                },
            )


def _handle_sweep_agent_jobs(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    payload: dict[str, Any],
) -> None:
    """stale な agent_jobs を timed_out/failed として終端化する。"""

    # --- payload から閾値/上限を読む（未指定時は定数既定） ---
    stale_seconds_raw = payload.get("stale_seconds")
    sweep_limit_raw = payload.get("limit")
    stale_seconds = max(
        1,
        int(stale_seconds_raw if stale_seconds_raw is not None else _AGENT_JOB_STALE_SECONDS),
    )
    sweep_limit = max(1, int(sweep_limit_raw if sweep_limit_raw is not None else 64))

    # --- stale 判定基準時刻 ---
    now_system_ts = _now_utc_ts()
    now_domain_ts = _now_domain_utc_ts()
    stale_before_ts = int(now_system_ts) - int(stale_seconds)

    timed_out_jobs = 0
    finalized_intents = 0
    skipped_jobs = 0
    finalized_result_ids_for_publish: list[str] = []
    timed_out_job_activity_rows: list[dict[str, Any]] = []

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- stale 候補（claimed/running）を古い heartbeat 順で取得 ---
        rows = db.execute(
            text(
                """
                SELECT job_id
                  FROM agent_jobs
                 WHERE status IN ('claimed','running')
                   AND COALESCE(heartbeat_at, updated_at, created_at) <= :stale_before_ts
                 ORDER BY COALESCE(heartbeat_at, updated_at, created_at) ASC, created_at ASC
                 LIMIT :limit
                """
            ),
            {
                "stale_before_ts": int(stale_before_ts),
                "limit": int(sweep_limit),
            },
        ).fetchall()

        # --- 各 stale job を timeout 終端化 ---
        for row in rows:
            job_id = str((row[0] if row else "") or "").strip()
            if not job_id:
                continue

            job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id)).one_or_none()
            if job is None:
                continue
            if str(job.status) not in {"claimed", "running"}:
                continue

            # --- job を timed_out に遷移 ---
            timeout_message = (
                f"agent runner heartbeat timeout (backend={str(job.backend)}, stale_seconds={int(stale_seconds)})"
            )
            timeout_details = {
                "agent_job_id": str(job.job_id),
                "backend": str(job.backend),
                "error_code": "agent_job_timeout",
                "error_message": str(timeout_message),
                "stale_seconds": int(stale_seconds),
                "runner_id": (str(job.runner_id) if job.runner_id is not None else None),
                "last_heartbeat_at": (int(job.heartbeat_at) if job.heartbeat_at is not None else None),
            }
            job.status = "timed_out"
            job.result_status = "failed"
            job.result_summary_text = str(timeout_message)
            job.result_details_json = common_utils.json_dumps(timeout_details)
            job.error_code = "agent_job_timeout"
            job.error_message = str(timeout_message)
            job.started_at = int(job.started_at or now_system_ts)
            job.finished_at = int(now_system_ts)
            job.updated_at = int(now_system_ts)
            timed_out_jobs += 1
            timed_out_job_activity_rows.append(
                {
                    "job_id": str(job.job_id),
                    "intent_id": str(job.intent_id),
                    "backend": str(job.backend),
                    "summary_text": str(timeout_message),
                }
            )

            # --- 対応 intent が running のときだけ ActionResult を保存して終端化 ---
            intent = db.query(Intent).filter(Intent.intent_id == str(job.intent_id)).one_or_none()
            if intent is None or str(intent.status) != "running":
                skipped_jobs += 1
                continue

            result_id = _finalize_intent_and_save_action_result(
                db=db,
                intent=intent,
                decision_id=str(job.decision_id),
                action_type="agent_delegate",
                capability_name="agent_delegate",
                result_status="failed",
                summary_text=str(timeout_message),
                result_payload_json=str(job.result_details_json or "{}"),
                useful_for_recall_hint=0,
                now_system_ts=int(now_system_ts),
                now_domain_ts=int(now_domain_ts),
                goal_id_hint=None,
                next_trigger=None,
            )
            finalized_result_ids_for_publish.append(str(result_id))
            finalized_intents += 1

    # --- DBコミット後に Console 向けイベントを配信する ---
    for result_id in list(finalized_result_ids_for_publish):
        try:
            _emit_autonomy_console_events_for_action_result(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                result_id=str(result_id),
                activity_state_override="timed_out",
            )
        except Exception:  # noqa: BLE001
            logger.exception("autonomy console publish failed result_id=%s", str(result_id))
    for row in list(timed_out_job_activity_rows):
        try:
            _publish_autonomy_activity(
                event_id=0,
                phase="agent_job",
                state="timed_out",
                action_type="agent_delegate",
                capability="agent_delegate",
                backend=str(row.get("backend") or ""),
                result_status="failed",
                summary_text=str(row.get("summary_text") or ""),
                decision_id=None,
                intent_id=str(row.get("intent_id") or ""),
                result_id=None,
                agent_job_id=str(row.get("job_id") or ""),
                goal_id=None,
            )
        except Exception:  # noqa: BLE001
            logger.exception("autonomy activity publish failed agent_job_id=%s", str(row.get("job_id") or ""))

    # --- 実際に回収が発生したときだけ warning を出す ---
    if int(timed_out_jobs) > 0:
        logger.warning(
            "sweep_agent_jobs timed out stale agent jobs timed_out=%s finalized_intents=%s skipped_jobs=%s stale_seconds=%s",
            int(timed_out_jobs),
            int(finalized_intents),
            int(skipped_jobs),
            int(stale_seconds),
        )


def _handle_snapshot_runtime(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    payload: dict[str, Any],
) -> None:
    """Runtime Blackboard のスナップショットを保存する。"""

    now_system_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 稼働中 intent を収集 ---
        active_intents = (
            db.query(Intent.intent_id)
            .filter(Intent.status.in_(["queued", "running", "blocked"]))
            .order_by(Intent.updated_at.desc())
            .limit(256)
            .all()
        )
        active_intent_ids = [str(r[0]) for r in active_intents if r and str(r[0] or "").strip()]

        # --- trigger/intents の滞留数 ---
        trigger_counts = db.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) AS claimed
                  FROM autonomy_triggers
                """
            )
        ).fetchone()
        intent_counts = db.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked
                  FROM intents
                """
            )
        ).fetchone()

        snapshot_kind = str(payload.get("snapshot_kind") or "periodic")
        runtime_bb_snapshot = get_runtime_blackboard().snapshot()
        attention_targets = []
        if isinstance(runtime_bb_snapshot, dict) and isinstance(runtime_bb_snapshot.get("attention_targets"), list):
            attention_targets = list(runtime_bb_snapshot.get("attention_targets") or [])
        snapshot_payload = {
            "active_intent_ids": active_intent_ids,
            "attention_targets": attention_targets,
            "trigger_counts": {
                "queued": int((trigger_counts[0] if trigger_counts and trigger_counts[0] is not None else 0) or 0),
                "claimed": int((trigger_counts[1] if trigger_counts and trigger_counts[1] is not None else 0) or 0),
            },
            "intent_counts": {
                "queued": int((intent_counts[0] if intent_counts and intent_counts[0] is not None else 0) or 0),
                "running": int((intent_counts[1] if intent_counts and intent_counts[1] is not None else 0) or 0),
                "blocked": int((intent_counts[2] if intent_counts and intent_counts[2] is not None else 0) or 0),
            },
            "worker_now_system_utc_ts": int(now_system_ts),
        }

        # --- RAM blackboard へも反映（DB snapshot と同じ内容を保持） ---
        get_runtime_blackboard().apply_snapshot_payload(
            snapshot_kind=str(snapshot_kind),
            payload=dict(snapshot_payload),
            created_at=int(now_system_ts),
        )

        db.add(
            RuntimeSnapshot(
                snapshot_kind=str(snapshot_kind),
                payload_json=common_utils.json_dumps(snapshot_payload),
                created_at=int(now_system_ts),
            )
        )

        # --- agent_jobs stale 回収ジョブを重複なく投入 ---
        existing_sweep = (
            db.query(Job.id)
            .filter(Job.kind == "sweep_agent_jobs")
            .filter(Job.status.in_([int(_JOB_PENDING), int(_JOB_RUNNING)]))
            .first()
        )
        if existing_sweep is None:
            db.add(
                Job(
                    kind="sweep_agent_jobs",
                    payload_json=common_utils.json_dumps(
                        {
                            "source": "snapshot_runtime",
                            "stale_seconds": int(_AGENT_JOB_STALE_SECONDS),
                            "limit": 64,
                        }
                    ),
                    status=int(_JOB_PENDING),
                    run_after=int(now_system_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_system_ts),
                    updated_at=int(now_system_ts),
                )
            )
