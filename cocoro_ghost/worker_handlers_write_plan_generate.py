"""
Workerジョブハンドラ（WritePlan生成）

役割:
- generate_write_plan の実処理を担当する。
"""

from __future__ import annotations

import json
from typing import Any

from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import Event, Job, State
from cocoro_ghost.time_utils import format_iso8601_local
from cocoro_ghost.worker_constants import JOB_PENDING as _JOB_PENDING
from cocoro_ghost.worker_handlers_common import _now_utc_ts


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
        # --- 検索対象外（自動分離など）なら、記憶更新の材料にしない ---
        if int(getattr(ev, "searchable", 1) or 0) != 1:
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
        max_tokens=2400,
    )
    content = ""
    try:
        content = str(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        try:
            content = str(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            content = ""
    plan_obj = common_utils.parse_first_json_object_or_raise(content)

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
