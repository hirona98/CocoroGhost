"""
階層型世界モデルの基盤ループ実行。

役割:
- Observe -> Strategize -> Tacticalize -> Execute -> Reflect を1サイクル実行する。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any

from cocoro_ghost.autonomy import runtime_control
from cocoro_ghost.autonomy.capability_adapters.base import AdapterExecutionContext, AdapterExecutionOutput
from cocoro_ghost.autonomy.capability_adapters.speak import SpeakCapabilityAdapter
from cocoro_ghost.autonomy.capability_adapters.web_access import WebAccessCapabilityAdapter
from cocoro_ghost.autonomy.capability_registry import (
    CapabilityDescriptor,
    CapabilityOperationDescriptor,
    CapabilityRegistry,
)
from cocoro_ghost.autonomy.contracts import ActionResultContract
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


def _extract_primary_user_text(source_event: dict[str, Any] | None) -> str:
    """戦術判断に使う主入力テキストを返す。"""

    # --- event が無い場合は空を返す ---
    if source_event is None:
        return ""

    # --- user_text を優先して使う ---
    user_text = str(source_event.get("user_text") or "").strip()
    if user_text:
        return user_text

    # --- 無ければ assistant_text を使う ---
    return str(source_event.get("assistant_text") or "").strip()


def _extract_first_url(text: str) -> str | None:
    """テキストから最初のURLを抽出する。"""

    # --- http/https URL を抽出 ---
    m = re.search(r"(https?://[^\s<>\")']+)", str(text or "").strip())
    if not m:
        return None
    return str(m.group(1) or "").strip() or None


def _should_search_web(text: str) -> bool:
    """Web検索を行うべき入力かを判定する。"""

    # --- 検索意図キーワードを判定 ---
    text_s = str(text or "").strip()
    if not text_s:
        return False
    keywords = ["検索", "調べて", "調査", "search", "web", "ウェブ"]
    return any(kw in text_s for kw in keywords)


def _build_web_search_query(text: str) -> str:
    """検索クエリを入力テキストから構築する。"""

    # --- 代表的な接頭辞を除去 ---
    q = str(text or "").strip()
    prefixes = ["検索:", "検索：", "調べて:", "調べて：", "search:"]
    for p in prefixes:
        if q.startswith(p):
            q = q[len(p) :].strip()
            break

    # --- 空になった場合は元文を使う ---
    if not q:
        return str(text or "").strip()
    return q


def _build_speak_message(*, trigger_type: str, source_event: dict[str, Any] | None, observation_text: str) -> str:
    """`speak.emit` 用メッセージを構築する。"""

    # --- event 起点なら観測内容を短く伝える ---
    if source_event is not None:
        message = f"いま考えていること: {observation_text}"
        return message[:300]

    # --- system/action_result 起点の既定文 ---
    if trigger_type == "action_result":
        return "さっきの行動結果をふまえて、次の方針を考えてるよ。"
    if trigger_type == "startup":
        return "起動したので、まず周囲の状況を観測するね。"
    return "いまの状況を観測して、次の行動を考えてるよ。"


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


def _ensure_core_capabilities(registry: CapabilityRegistry) -> None:
    """Phase 6/7 の基盤 capability を登録する。"""

    # --- `speak` descriptor を登録 ---
    registry.register_descriptor(
        descriptor=CapabilityDescriptor(
            capability_id="speak",
            display_name="Speak",
            enabled=True,
            version="1",
            metadata_json={"owner": "phase6"},
            operations=[
                CapabilityOperationDescriptor(
                    operation="emit",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["message"],
                        "properties": {
                            "message": {"type": "string"},
                            "target_client_id": {"type": "string"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["event_id", "source", "message"],
                        "properties": {
                            "event_id": {"type": "integer"},
                            "source": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "event_id"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "event_id": {"type": "integer"},
                            "source": {"type": "string"},
                            "stream_type": {"type": "string"},
                        },
                    },
                    timeout_seconds=10,
                    enabled=True,
                )
            ],
        )
    )

    # --- `web_access` descriptor を登録 ---
    registry.register_descriptor(
        descriptor=CapabilityDescriptor(
            capability_id="web_access",
            display_name="Web Access",
            enabled=True,
            version="1",
            metadata_json={"owner": "phase7"},
            operations=[
                CapabilityOperationDescriptor(
                    operation="search",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query", "top_k", "recency_days", "domains", "locale"],
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer"},
                            "recency_days": {},
                            "domains": {"type": "array"},
                            "locale": {"type": "string"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["items"],
                        "properties": {
                            "items": {"type": "array"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "query", "urls"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "query": {"type": "string"},
                            "urls": {"type": "array"},
                        },
                    },
                    timeout_seconds=20,
                    enabled=True,
                ),
                CapabilityOperationDescriptor(
                    operation="open_url",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url", "max_chars"],
                        "properties": {
                            "url": {"type": "string"},
                            "max_chars": {"type": "integer"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url", "title", "text", "fetched_at"],
                        "properties": {
                            "url": {"type": "string"},
                            "title": {"type": "string"},
                            "text": {"type": "string"},
                            "fetched_at": {"type": "string"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "url", "text_digest"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "url": {"type": "string"},
                            "text_digest": {"type": "string"},
                        },
                    },
                    timeout_seconds=20,
                    enabled=True,
                ),
                CapabilityOperationDescriptor(
                    operation="extract_structured",
                    input_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_url", "source_text", "target_schema_json"],
                        "properties": {
                            "source_url": {"type": "string"},
                            "source_text": {"type": "string"},
                            "target_schema_json": {"type": "object"},
                        },
                    },
                    result_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_url", "structured_data_json"],
                        "properties": {
                            "source_url": {"type": "string"},
                            "structured_data_json": {"type": "object"},
                        },
                    },
                    effect_schema_json={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["effect_type", "source_url", "keys"],
                        "properties": {
                            "effect_type": {"type": "string"},
                            "source_url": {"type": "string"},
                            "keys": {"type": "array"},
                        },
                    },
                    timeout_seconds=30,
                    enabled=True,
                ),
            ],
        )
    )

    # --- adapter を登録 ---
    registry.register_adapter(adapter=SpeakCapabilityAdapter())
    registry.register_adapter(adapter=WebAccessCapabilityAdapter())


def _decide_tactical_plan(
    *,
    trigger_type: str,
    source_event: dict[str, Any] | None,
    observation_text: str,
) -> dict[str, Any]:
    """Tacticalize の最小決定を返す。"""

    # --- event_created 以外は speak を使う ---
    if str(trigger_type) != "event_created":
        speak_message = _build_speak_message(
            trigger_type=trigger_type,
            source_event=source_event,
            observation_text=observation_text,
        )
        return {
            "goal_id": "autonomy_speak_observation",
            "goal_title": "観測を言語化する",
            "goal_intent": f"trigger_type={trigger_type} で最新観測を言語化する",
            "success_criteria": ["speak.emit を1回実行する"],
            "capability_id": "speak",
            "operation": "emit",
            "input_payload": {"message": str(speak_message)},
            "expected_effect": ["event.persisted"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- event入力本文を取得 ---
    input_text = _extract_primary_user_text(source_event)

    # --- URLを含む場合は open_url を優先 ---
    url = _extract_first_url(input_text)
    if url is not None:
        return {
            "goal_id": "autonomy_web_access",
            "goal_title": "観測をWeb参照で具体化する",
            "goal_intent": f"trigger_type={trigger_type} でURLを参照する",
            "success_criteria": ["web_access.open_url を1回実行する"],
            "capability_id": "web_access",
            "operation": "open_url",
            "input_payload": {"url": str(url), "max_chars": 8000},
            "expected_effect": ["web.page_opened"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- 検索意図を含む場合は search を使う ---
    if _should_search_web(input_text):
        query = _build_web_search_query(input_text)
        return {
            "goal_id": "autonomy_web_access",
            "goal_title": "観測をWeb検索で具体化する",
            "goal_intent": f"trigger_type={trigger_type} でWeb検索を実行する",
            "success_criteria": ["web_access.search を1回実行する"],
            "capability_id": "web_access",
            "operation": "search",
            "input_payload": {
                "query": str(query),
                "top_k": 5,
                "recency_days": None,
                "domains": [],
                "locale": "ja-JP",
            },
            "expected_effect": ["web.search_result"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- それ以外は speak を使う ---
    speak_message = _build_speak_message(
        trigger_type=trigger_type,
        source_event=source_event,
        observation_text=observation_text,
    )
    return {
        "goal_id": "autonomy_speak_observation",
        "goal_title": "観測を言語化する",
        "goal_intent": f"trigger_type={trigger_type} で最新観測を言語化する",
        "success_criteria": ["speak.emit を1回実行する"],
        "capability_id": "speak",
        "operation": "emit",
        "input_payload": {"message": str(speak_message)},
        "expected_effect": ["event.persisted"],
        "verify": ["wm_action_results.status=succeeded"],
    }


@dataclass(frozen=True)
class _ExecuteFailure(Exception):
    """Execute 失敗の分類付き例外。"""

    reason_code: str
    error_message: str

    def __str__(self) -> str:
        return str(self.error_message)


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


def _reflect_effects_into_world_model(
    *,
    store: WorldModelStore,
    observation_id: int,
    effects: list[dict[str, Any]],
    evidence_event_ids: list[int],
    trigger_type: str,
) -> None:
    """effect 群を world model へ反映する。"""

    # --- effect を順に処理 ---
    for effect in list(effects or []):
        if not isinstance(effect, dict):
            continue

        # --- URL候補を収集する ---
        urls: list[str] = []
        url_single = str(effect.get("url") or "").strip()
        if url_single:
            urls.append(url_single)
        for item in list(effect.get("urls") or []):
            url_item = str(item or "").strip()
            if url_item:
                urls.append(url_item)
        urls = sorted(set(urls))

        # --- URLを web_resource entity として保存 ---
        entity_ids_by_url: dict[str, int] = {}
        for url in list(urls or []):
            entity_id = store.upsert_entity(
                entity_key=f"web_resource:{url}",
                entity_type="web_resource",
                name=str(url),
                value_json={"url": str(url)},
                confidence=0.8,
            )
            entity_ids_by_url[str(url)] = int(entity_id)
            store.upsert_link(
                link_type="evidence_for",
                from_type="observation",
                from_id=str(int(observation_id)),
                to_type="entity",
                to_id=str(int(entity_id)),
                confidence=0.8,
                evidence_event_ids=list(evidence_event_ids),
            )

        # --- query + urls を belief として保存 ---
        query = str(effect.get("query") or "").strip()
        if query and urls:
            store.add_belief(
                subject_entity_id=None,
                predicate="web.search.query",
                object_text=str(query),
                value_json={
                    "urls": list(urls),
                    "trigger_type": str(trigger_type),
                },
                confidence=0.7,
                source_type="action_result",
                evidence_event_ids=list(evidence_event_ids),
            )

        # --- text_digest を URL entity に紐付ける ---
        text_digest = str(effect.get("text_digest") or "").strip()
        if text_digest and url_single:
            store.add_belief(
                subject_entity_id=entity_ids_by_url.get(str(url_single)),
                predicate="web.page.digest",
                object_text=str(text_digest),
                value_json={"url": str(url_single)},
                confidence=0.6,
                source_type="action_result",
                evidence_event_ids=list(evidence_event_ids),
            )

        # --- structured keys を belief に保存 ---
        keys = [str(k).strip() for k in list(effect.get("keys") or []) if str(k).strip()]
        source_url = str(effect.get("source_url") or "").strip()
        if keys and source_url:
            store.add_belief(
                subject_entity_id=entity_ids_by_url.get(str(source_url)),
                predicate="web.structured.keys",
                object_text=",".join(sorted(set(keys))),
                value_json={"source_url": str(source_url)},
                confidence=0.65,
                source_type="action_result",
                evidence_event_ids=list(evidence_event_ids),
            )


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
        _ensure_core_capabilities(registry)

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
        tactical = _decide_tactical_plan(
            trigger_type=trigger_type,
            source_event=source_event,
            observation_text=observation_text,
        )
        goal_id = str(tactical["goal_id"])
        capability_id = str(tactical["capability_id"])
        operation = str(tactical["operation"])
        input_payload = dict(tactical["input_payload"])
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
            preconditions=[],
            expected_effect=list(expected_effect),
            verify=list(verify),
            issued_at=int(now_ts),
            deadline_at=int(now_ts) + 20,
        )

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
        _reflect_effects_into_world_model(
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
