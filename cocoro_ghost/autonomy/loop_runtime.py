"""
階層型世界モデルの基盤ループ実行。

役割:
- Observe -> Strategize -> Tacticalize -> Execute -> Reflect を1サイクル実行する。
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from cocoro_ghost.autonomy import runtime_control
from cocoro_ghost.autonomy.capability_adapters.base import AdapterExecutionContext, AdapterExecutionOutput
from cocoro_ghost.autonomy.capability_bootstrap import register_default_capabilities
from cocoro_ghost.autonomy.capability_registry import CapabilityRegistry
from cocoro_ghost.autonomy.contracts import ActionResultContract
from cocoro_ghost.autonomy.effect_reflector import reflect_effects_into_world_model
from cocoro_ghost.autonomy.tactical_planner import decide_tactical_plan
from cocoro_ghost.autonomy.world_model_store import WorldModelStore
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.memory_models import Event


def _now_utc_ts() -> int:
    """現在UTC時刻をUNIX秒で返す。"""

    return int(time.time())


def _extract_event_text(ev: dict[str, Any]) -> str:
    """Event から観測用テキストを抽出する。"""

    # --- ユーザー発話と応答を順に結合 ---
    parts: list[str] = []
    user_text = str(ev.get("user_text") or "").strip()
    assistant_text = str(ev.get("assistant_text") or "").strip()
    if user_text:
        parts.append(user_text)
    if assistant_text:
        parts.append(assistant_text)

    # --- 何も無い場合は source を使う ---
    if not parts:
        return f"(source={str(ev.get('source') or '').strip()})"

    # --- 長すぎる観測は切り詰める ---
    text_out = "\n".join(parts).strip()
    if len(text_out) > 1200:
        text_out = text_out[:1200]
    return text_out


def _load_source_event(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    event_id: int | None,
) -> dict[str, Any] | None:
    """観測起点に使う event を取得する。"""

    # --- 指定 event_id があれば優先する ---
    if event_id is not None and int(event_id) > 0:
        with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
            found = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
            if found is None:
                return None
            return {
                "event_id": int(found.event_id),
                "source": str(found.source or ""),
                "user_text": str(found.user_text or ""),
                "assistant_text": str(found.assistant_text or ""),
            }

    # --- 無指定時は最新 event を使う ---
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        found = db.query(Event).order_by(Event.created_at.desc(), Event.event_id.desc()).first()
        if found is None:
            return None
        return {
            "event_id": int(found.event_id),
            "source": str(found.source or ""),
            "user_text": str(found.user_text or ""),
            "assistant_text": str(found.assistant_text or ""),
        }


@dataclass(frozen=True)
class _ExecuteFailure(Exception):
    """Execute 失敗の分類付き例外。"""

    reason_code: str
    error_message: str

    def __str__(self) -> str:
        return str(self.error_message)


def _are_preconditions_satisfied(*, preconditions: list[str], input_payload: dict[str, Any]) -> bool:
    """precondition 配列を評価する。"""

    # --- preconditions を順に評価 ---
    for pre in list(preconditions or []):
        token = str(pre or "").strip()
        if token == "url_present":
            if not str(input_payload.get("url") or "").strip():
                return False
            continue
        if token == "source_url_present":
            if not str(input_payload.get("source_url") or "").strip():
                return False
            continue
        if token == "source_text_present":
            if not str(input_payload.get("source_text") or "").strip():
                return False
            continue
        # --- 未知preconditionは失敗扱い ---
        return False
    return True


def _execute_ticket_once(
    *,
    registry: CapabilityRegistry,
    ticket_id: str,
    goal_id: str,
    capability_id: str,
    operation: str,
    input_payload: dict[str, Any],
    trigger_type: str,
    issued_at: int,
    embedding_preset_id: str,
    embedding_dimension: int,
) -> AdapterExecutionOutput:
    """ticket を1回実行して adapter 出力を返す。"""

    # --- operation descriptor を解決 ---
    try:
        op_desc = registry.resolve_operation(capability_id=capability_id, operation=operation)
    except Exception as exc:  # noqa: BLE001
        raise _ExecuteFailure(reason_code="execute_adapter_not_found", error_message=str(exc)) from exc

    # --- 入力 schema を検証 ---
    try:
        registry.validate_input_payload(capability_id=capability_id, operation=operation, payload=input_payload)
    except Exception as exc:  # noqa: BLE001
        raise _ExecuteFailure(reason_code="execute_schema_invalid", error_message=str(exc)) from exc

    # --- adapter を解決 ---
    try:
        adapter = registry.resolve_adapter(capability_id=capability_id)
    except Exception as exc:  # noqa: BLE001
        raise _ExecuteFailure(reason_code="execute_adapter_not_found", error_message=str(exc)) from exc

    # --- adapter を1回実行 ---
    try:
        output = adapter.execute(
            context=AdapterExecutionContext(
                goal_id=str(goal_id),
                ticket_id=str(ticket_id),
                capability_id=str(capability_id),
                operation=str(operation),
                trigger_type=str(trigger_type),
                issued_at=int(issued_at),
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
            ),
            input_payload=dict(input_payload or {}),
            timeout_seconds=int(op_desc.timeout_seconds),
        )
    except Exception as exc:  # noqa: BLE001
        raise _ExecuteFailure(reason_code="execute_runtime_error", error_message=str(exc)) from exc

    # --- result/effect schema を検証 ---
    try:
        registry.validate_result_payload(
            capability_id=capability_id,
            operation=operation,
            payload=dict(output.result_payload or {}),
        )
        registry.validate_effects(
            capability_id=capability_id,
            operation=operation,
            effects=list(output.effects or []),
        )
    except Exception as exc:  # noqa: BLE001
        raise _ExecuteFailure(reason_code="execute_schema_invalid", error_message=str(exc)) from exc

    return output


def run_autonomy_cycle(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """自律ループを1サイクル実行する。"""

    # --- 未使用依存を明示 ---
    _ = llm_client

    # --- サイクル開始を記録 ---
    cycle_started_at = _now_utc_ts()
    runtime_control.mark_cycle_started(started_at=int(cycle_started_at))

    # --- 失敗時にも使うローカル状態 ---
    trigger_type = str(payload.get("trigger_type") or "periodic").strip()
    store: WorldModelStore | None = None
    goal_id = "autonomy_speak_observation"
    ticket_id: str | None = None
    observation_id: int | None = None

    # --- 失敗時も終了情報を残す ---
    cycle_status = "failed"
    try:
        # --- 入力を正規化 ---
        source_event_id_raw = payload.get("event_id")
        source_event_id = int(source_event_id_raw) if source_event_id_raw is not None else None
        source_result_id = str(payload.get("result_id") or "").strip()
        if trigger_type == "action_result" and not source_result_id:
            raise RuntimeError("result_id is required for trigger_type=action_result")
        now_ts = _now_utc_ts()

        # --- 依存オブジェクトを作成 ---
        store = WorldModelStore(
            embedding_preset_id=str(embedding_preset_id),
            embedding_dimension=int(embedding_dimension),
        )
        registry = CapabilityRegistry(
            embedding_preset_id=str(embedding_preset_id),
            embedding_dimension=int(embedding_dimension),
        )

        # --- capability 契約を保証 ---
        register_default_capabilities(registry=registry)

        # --- source event を取得 ---
        source_event = _load_source_event(
            embedding_preset_id=str(embedding_preset_id),
            embedding_dimension=int(embedding_dimension),
            event_id=source_event_id,
        )
        source_type = "system"
        source_ref = None
        observation_text = f"autonomy tick trigger_type={trigger_type}"
        if source_event is not None:
            source_type = "event"
            source_ref = f"event:{int(source_event['event_id'])}"
            observation_text = _extract_event_text(source_event)
        elif trigger_type == "action_result":
            source_type = "action_result"
            source_ref = f"result:{source_result_id}"
            observation_text = f"autonomy action_result result_id={source_result_id}"

        # --- Observe: 観測を保存 ---
        observation_id = store.add_observation(
            source_type=source_type,
            source_ref=source_ref,
            content_text=observation_text,
            payload_json={
                "trigger_type": trigger_type,
                "source_type": source_type,
                "result_id": (source_result_id if source_result_id else None),
            },
        )

        # --- Observe: source event がある場合は entity を upsert ---
        source_entity_id: int | None = None
        if source_event is not None:
            source_entity_id = store.upsert_entity(
                entity_key=f"event:{int(source_event['event_id'])}",
                entity_type="event",
                name=f"event:{int(source_event['event_id'])}",
                value_json={
                    "event_id": int(source_event["event_id"]),
                    "source": str(source_event.get("source") or ""),
                },
                confidence=1.0,
            )

        # --- Tacticalize: 実行計画を決定 ---
        tactical = decide_tactical_plan(
            trigger_type=trigger_type,
            source_event=source_event,
            observation_text=observation_text,
        )
        goal_id = str(tactical["goal_id"])
        capability_id = str(tactical["capability_id"])
        operation = str(tactical["operation"])
        input_payload = dict(tactical["input_payload"])
        preconditions = list(tactical["preconditions"])
        expected_effect = list(tactical["expected_effect"])
        verify = list(tactical["verify"])

        # --- Strategize: 目標を更新 ---
        store.upsert_goal(
            goal_id=goal_id,
            title=str(tactical["goal_title"]),
            intent=str(tactical["goal_intent"]),
            priority=0.6,
            horizon_seconds=3600,
            success_criteria=list(tactical["success_criteria"]),
            constraints=["同期チャット経路に重い処理を入れない"],
            status="active",
        )

        # --- Tacticalize: ticket を発行 ---
        ticket_id = store.add_action_ticket(
            goal_id=goal_id,
            capability_id=str(capability_id),
            operation=str(operation),
            input_payload=dict(input_payload),
            preconditions=list(preconditions),
            expected_effect=list(expected_effect),
            verify=list(verify),
            issued_at=int(now_ts),
            deadline_at=int(now_ts) + 20,
        )

        # --- precondition 未成立は cancelled で確定 ---
        if not _are_preconditions_satisfied(preconditions=list(preconditions), input_payload=dict(input_payload)):
            store.update_action_ticket_status(
                ticket_id=str(ticket_id),
                status="cancelled",
                reason_code="ticket_precondition_failed",
            )
            cycle_status = "succeeded"
            return {
                "status": "cancelled",
                "trigger_type": trigger_type,
                "source_type": source_type,
                "goal_id": goal_id,
                "capability_id": str(capability_id),
                "operation": str(operation),
                "ticket_id": str(ticket_id),
                "observation_id": int(observation_id),
                "reason_code": "ticket_precondition_failed",
            }

        # --- Execute: running へ遷移 ---
        store.update_action_ticket_status(ticket_id=str(ticket_id), status="running", reason_code=None)

        # --- Execute: adapter 実行 ---
        output = _execute_ticket_once(
            registry=registry,
            ticket_id=str(ticket_id),
            goal_id=goal_id,
            capability_id=str(capability_id),
            operation=str(operation),
            input_payload=dict(input_payload),
            trigger_type=trigger_type,
            issued_at=int(now_ts),
            embedding_preset_id=str(embedding_preset_id),
            embedding_dimension=int(embedding_dimension),
        )

        # --- Execute: 成功結果を保存 ---
        finished_at = _now_utc_ts()
        result = ActionResultContract(
            ticket_id=str(ticket_id),
            status="succeeded",
            observations=[{"observation_id": int(observation_id)}],
            effects=list(output.effects or []),
            error_message=None,
            reason_code=None,
            finished_at=int(finished_at),
        )
        store.update_action_ticket_status(ticket_id=str(ticket_id), status="succeeded", reason_code=None)
        result_id = store.add_action_result(
            ticket_id=str(ticket_id),
            status=result.status,
            observations=list(result.observations),
            effects=list(result.effects),
            error_message=result.error_message,
            reason_code=result.reason_code,
            finished_at=int(result.finished_at),
        )

        # --- Reflect: 共通反映（goal/ticketとeffect） ---
        evidence_ids: list[int] = []
        if source_event is not None:
            evidence_ids = [int(source_event["event_id"])]

        store.add_belief(
            subject_entity_id=source_entity_id,
            predicate="observation.action_executed",
            object_text=f"observation_id={int(observation_id)}",
            value_json={
                "result_id": str(result_id),
                "trigger_type": trigger_type,
                "capability_id": str(capability_id),
                "operation": str(operation),
            },
            confidence=0.75,
            source_type="action_result",
            evidence_event_ids=evidence_ids,
        )
        if source_event is not None:
            store.upsert_link(
                link_type="evidence_for",
                from_type="event",
                from_id=str(int(source_event["event_id"])),
                to_type="observation",
                to_id=str(int(observation_id)),
                confidence=1.0,
                evidence_event_ids=evidence_ids,
            )

        # --- Reflect: effect を world model へ反映 ---
        reflect_effects_into_world_model(
            store=store,
            observation_id=int(observation_id),
            effects=list(result.effects or []),
            evidence_event_ids=list(evidence_ids),
            trigger_type=str(trigger_type),
        )

        cycle_status = "succeeded"
        return {
            "status": "succeeded",
            "trigger_type": trigger_type,
            "source_type": source_type,
            "goal_id": goal_id,
            "capability_id": str(capability_id),
            "operation": str(operation),
            "ticket_id": str(ticket_id),
            "observation_id": int(observation_id),
            "result_id": str(result_id),
        }
    except _ExecuteFailure as exc:
        # --- Execute失敗を結果として保存する ---
        now_ts = _now_utc_ts()
        result_id = None
        if store is not None and ticket_id is not None:
            store.update_action_ticket_status(ticket_id=str(ticket_id), status="failed", reason_code=str(exc.reason_code))
            result_id = store.add_action_result(
                ticket_id=str(ticket_id),
                status="failed",
                observations=([{"observation_id": int(observation_id)}] if observation_id is not None else []),
                effects=[],
                error_message=str(exc.error_message),
                reason_code=str(exc.reason_code),
                finished_at=int(now_ts),
            )
        cycle_status = "failed"
        return {
            "status": "failed",
            "trigger_type": trigger_type,
            "goal_id": goal_id,
            "ticket_id": (str(ticket_id) if ticket_id is not None else None),
            "observation_id": (int(observation_id) if observation_id is not None else None),
            "result_id": (str(result_id) if result_id is not None else None),
            "reason_code": str(exc.reason_code),
            "error": str(exc.error_message),
        }
    except Exception as exc:  # noqa: BLE001
        # --- 予期しない失敗は RuntimeError 扱い ---
        cycle_status = "failed"
        return {
            "status": "failed",
            "trigger_type": str(payload.get("trigger_type") or "periodic"),
            "reason_code": "execute_runtime_error",
            "error": str(exc),
        }
    finally:
        # --- サイクル終了を記録 ---
        runtime_control.mark_cycle_finished(
            finished_at=_now_utc_ts(),
            status=("succeeded" if cycle_status == "succeeded" else "failed"),
        )
