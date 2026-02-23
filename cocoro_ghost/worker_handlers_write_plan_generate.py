"""
Workerジョブハンドラ（WritePlan生成）

役割:
- generate_write_plan の実処理を担当する。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import ActionDecision, ActionResult, Event, Job, State, WorldModelItem
from cocoro_ghost.time_utils import format_iso8601_local
from cocoro_ghost.worker_constants import JOB_PENDING as _JOB_PENDING
from cocoro_ghost.worker_handlers_common import _now_utc_ts


logger = logging.getLogger(__name__)


def _response_finish_reason(resp: Any) -> str:
    """
    LLMレスポンスから finish_reason を取り出す。

    調査ログで「length / stop」を判別するために使う。
    """

    # --- オブジェクト形式を優先して読む ---
    try:
        choice = resp.choices[0]
        value = getattr(choice, "finish_reason", None) or choice.get("finish_reason")
        return str(value or "")
    except Exception:  # noqa: BLE001
        pass

    # --- dict形式でも読む ---
    try:
        value = resp["choices"][0].get("finish_reason")
        return str(value or "")
    except Exception:  # noqa: BLE001
        return ""


def _handle_generate_write_plan(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """WritePlan を生成し、apply_write_plan ジョブを投入する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")

    # --- ランタイム設定（ペルソナ/呼称）を取得する ---
    # NOTE:
    # - WritePlan は内部用だが、記憶（state_updates / event_affect）に残る文章は人格の口調に揃える。
    # - 初期化順や例外の影響を避けるため、取れない場合は最小の既定値へフォールバックする。
    persona_text = ""
    addon_text = ""
    second_person_label = "あなた"
    try:
        from cocoro_ghost.config import get_config_store

        cfg = get_config_store().config
        persona_text = str(getattr(cfg, "persona_text", "") or "")
        addon_text = str(getattr(cfg, "addon_text", "") or "")
        second_person_label = str(getattr(cfg, "second_person_label", "") or "").strip() or "あなた"
    except Exception:  # noqa: BLE001
        persona_text = ""
        addon_text = ""
        second_person_label = "あなた"

    # --- event + 周辺コンテキストを集める（過剰に重くしない） ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        # --- 検索対象外でも autonomy の内部イベントは記憶更新対象にする ---
        # NOTE:
        # - `deliberation_decision` / `action_result` は searchable=0 が正だが、
        #   state/world_model 更新の材料としては扱う必要がある。
        if (
            int(getattr(ev, "searchable", 1) or 0) != 1
            and str(ev.source) not in {"deliberation_decision", "action_result"}
        ):
            return

        # --- 画像要約（events.image_summaries_json）をlist[str]へ正規化する ---
        # NOTE:
        # - 画像そのものは保存しないが、要約は状態更新/文脈/要約に効かせるため、WritePlan入力へ含める。
        def _image_summaries_list(image_summaries_json: Any) -> list[str]:
            s = str(image_summaries_json or "").strip()
            if not s:
                return []
            try:
                obj = json.loads(s)
            except Exception:  # noqa: BLE001
                return []
            if not isinstance(obj, list):
                return []
            return [str(x or "").strip()[:400] for x in obj if str(x or "").strip()]

        # --- client_context_json をdictへ正規化する（観測の手がかりとして渡す） ---
        def _client_context_dict(client_context_json: Any) -> dict[str, Any] | None:
            s = str(client_context_json or "").strip()
            if not s:
                return None
            try:
                obj = json.loads(s)
            except Exception:  # noqa: BLE001
                return None
            if not isinstance(obj, dict):
                return None
            # NOTE: 過剰に膨らませない（必要最小限）。
            return {
                "active_app": str(obj.get("active_app") or "").strip(),
                "window_title": str(obj.get("window_title") or "").strip(),
                "locale": str(obj.get("locale") or "").strip(),
            }

        # --- 観測メタ（desktop_watch/vision_detail向け） ---
        observation_meta: dict[str, Any] | None = None
        if str(ev.source) in {"desktop_watch", "vision_detail"}:
            # NOTE: 行為主体の誤認防止のため、二人称呼称は設定値で明示する（ペルソナ本文からはパースしない）。
            observation_meta = {
                "observer": "self",
                "second_person_label": str(second_person_label),
                "user_text_role": "screen_description",
            }

        event_snapshot = {
            "event_id": int(ev.event_id),
            "created_at": format_iso8601_local(int(ev.created_at)),
            "source": str(ev.source),
            "client_id": (str(ev.client_id) if ev.client_id is not None else None),
            "user_text": str(ev.user_text or ""),
            "assistant_text": str(ev.assistant_text or ""),
            "image_summaries": _image_summaries_list(getattr(ev, "image_summaries_json", None)),
            "client_context": _client_context_dict(getattr(ev, "client_context_json", None)),
            "observation": observation_meta,
        }

        # --- source別の構造化参照（autonomy系） ---
        if str(ev.source) == "deliberation_decision":
            decision_row = (
                db.query(ActionDecision)
                .filter(ActionDecision.event_id == int(ev.event_id))
                .one_or_none()
            )
            if decision_row is not None:
                event_snapshot["deliberation_decision"] = {
                    "decision_id": str(decision_row.decision_id),
                    "trigger_type": str(decision_row.trigger_type),
                    "trigger_ref": (str(decision_row.trigger_ref) if decision_row.trigger_ref is not None else None),
                    "decision_outcome": str(decision_row.decision_outcome),
                    "action_type": (str(decision_row.action_type) if decision_row.action_type is not None else None),
                    "action_payload_json": (
                        str(decision_row.action_payload_json) if decision_row.action_payload_json is not None else None
                    ),
                    "reason_text": (str(decision_row.reason_text) if decision_row.reason_text is not None else None),
                    "confidence": float(decision_row.confidence),
                }
        world_model_state_promotions: list[dict[str, Any]] = []
        if str(ev.source) == "action_result":
            result_row = (
                db.query(ActionResult)
                .filter(ActionResult.event_id == int(ev.event_id))
                .one_or_none()
            )
            if result_row is not None:
                event_snapshot["action_result"] = {
                    "result_id": str(result_row.result_id),
                    "intent_id": (str(result_row.intent_id) if result_row.intent_id is not None else None),
                    "decision_id": (str(result_row.decision_id) if result_row.decision_id is not None else None),
                    "capability_name": (
                        str(result_row.capability_name) if result_row.capability_name is not None else None
                    ),
                    "result_status": str(result_row.result_status),
                    "summary_text": (str(result_row.summary_text) if result_row.summary_text is not None else None),
                    "result_payload_json": str(result_row.result_payload_json),
                    "useful_for_recall_hint": bool(int(result_row.useful_for_recall_hint)),
                }

                # --- world_model_items の昇格候補を deterministic に作る ---
                # NOTE:
                # - 最終判定責務は generate_write_plan に置く（LLM出力に依存しない）。
                # - apply_write_plan は state_updates を適用するだけにする。
                now_domain_ts = int(ev.created_at)
                wm_rows = (
                    db.query(WorldModelItem)
                    .filter(WorldModelItem.source_result_id == str(result_row.result_id))
                    .order_by(WorldModelItem.updated_at.desc(), WorldModelItem.item_id.asc())
                    .limit(16)
                    .all()
                )
                for wm in wm_rows:
                    if str(wm.observation_class or "") != "fact":
                        continue
                    if int(wm.active or 0) != 1:
                        continue
                    if float(wm.confidence or 0.0) < 0.80:
                        continue
                    if int(now_domain_ts) - int(wm.freshness_at or 0) > 604800:
                        continue
                    if not (
                        int(wm.observation_count or 0) >= 2
                        or float(wm.confidence or 0.0) >= 0.93
                    ):
                        continue

                    # --- JSON を読みやすい payload へ整形 ---
                    entity_obj = common_utils.json_loads_maybe(str(wm.entity_json or "{}"))
                    relation_obj = common_utils.json_loads_maybe(str(wm.relation_json or "{}"))
                    location_obj = common_utils.json_loads_maybe(str(wm.location_json or "{}"))
                    affordance_obj = common_utils.json_loads_maybe(str(wm.affordance_json or "{}"))
                    if not isinstance(entity_obj, dict):
                        entity_obj = {}
                    if not isinstance(relation_obj, dict):
                        relation_obj = {}
                    if not isinstance(location_obj, dict):
                        location_obj = {}
                    if not isinstance(affordance_obj, dict):
                        affordance_obj = {}

                    # --- 同じ world_model_item を表す state を探して upsert する ---
                    marker = f"\"world_model_item_id\":\"{str(wm.item_id)}\""
                    existing_state = (
                        db.query(State)
                        .filter(State.kind == "fact")
                        .filter(State.payload_json.like(f"%{marker}%"))
                        .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                        .first()
                    )

                    summary_text = str(affordance_obj.get("summary") or "").strip()
                    if not summary_text:
                        summary_text = str(result_row.summary_text or "").strip()
                    if not summary_text:
                        summary_text = f"web調査メモ: {str(wm.item_key)}"

                    world_model_state_promotions.append(
                        {
                            "op": "upsert",
                            "kind": "fact",
                            "state_id": (int(existing_state.state_id) if existing_state is not None else None),
                            "body_text": str(summary_text)[:2000],
                            "payload": {
                                "world_model_item_id": str(wm.item_id),
                                "item_key": str(wm.item_key),
                                "observation_class": str(wm.observation_class or ""),
                                "observation_count": int(wm.observation_count or 0),
                                "freshness_at": int(wm.freshness_at or 0),
                                "entity": entity_obj,
                                "relation": relation_obj,
                                "location": location_obj,
                                "affordance": affordance_obj,
                            },
                            "confidence": float(wm.confidence or 0.0),
                            "valid_from_ts": int(wm.freshness_at or 0),
                            "valid_to_ts": None,
                            "reason": "world_model_items から state.fact へ昇格した",
                            "evidence_event_ids": [int(ev.event_id)],
                            "entities": [],
                        }
                    )

        # --- 直近の出来事（同一client優先） ---
        recent_events_q = db.query(Event).filter(Event.searchable == 1).order_by(Event.event_id.desc())
        if ev.client_id is not None and str(ev.client_id).strip():
            recent_events_q = recent_events_q.filter(Event.client_id == str(ev.client_id))
        recent_events = recent_events_q.limit(12).all()

        # --- 直近の状態 ---
        recent_states = (
            db.query(State)
            .filter(State.searchable == 1)
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .limit(24)
            .all()
        )

        recent_event_snapshots = [
            {
                "event_id": int(x.event_id),
                "created_at": format_iso8601_local(int(x.created_at)),
                "source": str(x.source),
                "user_text": str(x.user_text or "")[:1000],
                "assistant_text": str(x.assistant_text or "")[:1000],
                "image_summaries_preview": (
                    "\n".join(_image_summaries_list(getattr(x, "image_summaries_json", None)))[:1000] or None
                ),
            }
            for x in recent_events
        ]
        recent_state_snapshots = [
            {
                "state_id": int(s.state_id),
                "kind": str(s.kind),
                "body_text": str(s.body_text)[:1000],
                "payload_json": str(s.payload_json)[:1000],
                "last_confirmed_at": format_iso8601_local(int(s.last_confirmed_at)),
                # NOTE: DBはUNIX秒だが、LLMにはISOで渡す。
                "valid_from_ts": (
                    format_iso8601_local(int(s.valid_from_ts))
                    if s.valid_from_ts is not None and int(s.valid_from_ts) > 0
                    else None
                ),
                "valid_to_ts": (
                    format_iso8601_local(int(s.valid_to_ts))
                    if s.valid_to_ts is not None and int(s.valid_to_ts) > 0
                    else None
                ),
            }
            for s in recent_states
        ]

    input_obj: dict[str, Any] = {
        "event": event_snapshot,
        "recent_events": recent_event_snapshots,
        "recent_states": recent_state_snapshots,
    }

    # --- LLMでWritePlanを作る ---
    resp = llm_client.generate_json_response(
        system_prompt=prompt_builders.write_plan_system_prompt(
            persona_text=persona_text,
            second_person_label=second_person_label,
        ),
        input_text=common_utils.json_dumps(input_obj),
        purpose=LlmRequestPurpose.ASYNC_WRITE_PLAN,
        max_tokens=2000,
    )
    content = ""
    try:
        content = str(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        try:
            content = str(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            content = ""
    # --- JSON抽出失敗時は、レート制限起因かどうかを warning に明示する ---
    try:
        plan_obj = common_utils.parse_first_json_object_or_raise(content)
    except Exception as exc:  # noqa: BLE001
        finish_reason = _response_finish_reason(resp)
        content_text = str(content or "")
        rate_limit_hint = llm_client.response_rate_limit_hint(resp)
        logger.warning(
            (
                "generate_write_plan JSON parse failed "
                "(event_id=%s, finish_reason=%s, chars=%s, error=%s)%s"
            ),
            int(event_id),
            str(finish_reason),
            int(len(content_text)),
            str(exc),
            (f" cause={str(rate_limit_hint)}" if rate_limit_hint else ""),
        )
        raise

    # --- deterministic: world_model 昇格を state_updates へ合成 ---
    if world_model_state_promotions:
        su = plan_obj.get("state_updates") if isinstance(plan_obj, dict) else None
        if not isinstance(su, list):
            plan_obj["state_updates"] = []
            su = plan_obj["state_updates"]
        for item in world_model_state_promotions:
            su.append(item)

    # --- apply_write_plan を投入する（planはpayloadに入れて監査できるようにする） ---
    now_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.add(
            Job(
                kind="apply_write_plan",
                payload_json=common_utils.json_dumps({"event_id": int(event_id), "write_plan": plan_obj}),
                status=int(_JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )
