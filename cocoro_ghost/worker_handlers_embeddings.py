"""
Workerジョブハンドラ（埋め込み系）

役割:
- events/state/event_affect の埋め込み更新
- assistant要約の生成・保存
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils, prompt_builders, vector_index
from cocoro_ghost.db import memory_session_scope, upsert_vec_item
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import Event, EventAffect, EventAssistantSummary
from cocoro_ghost.worker_handlers_common import (
    _build_event_affect_embedding_text,
    _build_event_assistant_summary_input,
    _build_event_embedding_text,
    _build_state_embedding_text,
    _now_utc_ts,
)


def _handle_upsert_event_embedding(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """events の埋め込みを生成し vec_items へ upsert する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")

    # --- event を読む ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        # --- 検索対象外（自動分離など）なら、vec_items を消して終了 ---
        # NOTE:
        # - vec_items が残ると、ベクトル検索で再浮上する可能性がある。
        # - searchable=0 は「ログは残すが、想起には使わない」を表す。
        if int(getattr(ev, "searchable", 1) or 0) != 1:
            item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT), int(event_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_event_embedding_text(ev)
        rank_at = int(ev.created_at)

    if not text_in:
        return

    # --- embedding を作る ---
    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_EVENT_EMBEDDING)[0]

    # --- vec_items へ書く（kind+entity_idの衝突を避ける） ---
    item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT), int(event_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(vector_index.VEC_KIND_EVENT),
            rank_at=int(rank_at),
            active=1,
        )


def _handle_upsert_event_assistant_summary(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """events.assistant_text の要約を生成し、event_assistant_summaries へ upsert する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")

    # --- event を読む（必要な材料だけ） ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return

        # --- 本文が無いなら要約しない ---
        assistant_text = str(ev.assistant_text or "").strip()
        if not assistant_text:
            return

        # --- 既に最新の要約があるならスキップ ---
        existing = db.query(EventAssistantSummary).filter(EventAssistantSummary.event_id == int(event_id)).one_or_none()
        if existing is not None:
            if int(existing.event_updated_at) == int(ev.updated_at) and str(existing.summary_text or "").strip():
                return

        input_text = _build_event_assistant_summary_input(ev)
        event_updated_at = int(ev.updated_at)

    if not input_text:
        return

    # --- LLMで要約（JSONのみ） ---
    resp = llm_client.generate_json_response(
        system_prompt=prompt_builders.event_assistant_summary_system_prompt(),
        input_text=input_text,
        purpose=LlmRequestPurpose.ASYNC_EVENT_ASSISTANT_SUMMARY,
        max_tokens=300,
    )
    obj = common_utils.parse_first_json_object_or_none(common_utils.first_choice_content(resp))
    summary = str((obj or {}).get("summary") or "").strip()
    summary = common_utils.strip_face_tags(summary)
    summary = " ".join(summary.replace("\r", " ").replace("\n", " ").split()).strip()

    # --- 空/異常は保存しない（ノイズを増やさない） ---
    if not summary:
        return
    if len(summary) > 280:
        summary = summary[:280].rstrip()

    # --- DBへ upsert（event_id 1:1） ---
    now_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.execute(
            text(
                """
                INSERT INTO event_assistant_summaries(event_id, summary_text, event_updated_at, created_at, updated_at)
                VALUES (:event_id, :summary_text, :event_updated_at, :created_at, :updated_at)
                ON CONFLICT(event_id) DO UPDATE SET
                    summary_text=excluded.summary_text,
                    event_updated_at=excluded.event_updated_at,
                    updated_at=excluded.updated_at
                """
            ),
            {
                "event_id": int(event_id),
                "summary_text": str(summary),
                "event_updated_at": int(event_updated_at),
                "created_at": int(now_ts),
                "updated_at": int(now_ts),
            },
        )


def _handle_upsert_state_embedding(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """state の埋め込みを生成し vec_items へ upsert する。"""

    state_id = int(payload.get("state_id") or 0)
    if state_id <= 0:
        raise RuntimeError("payload.state_id is required")

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        st = db.query(State).filter(State.state_id == int(state_id)).one_or_none()
        if st is None:
            return
        # --- 検索対象外（自動分離など）なら、vec_items を消して終了 ---
        if int(getattr(st, "searchable", 1) or 0) != 1:
            item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_STATE), int(state_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_state_embedding_text(st)
        rank_at = int(st.last_confirmed_at)
        payload_obj = common_utils.json_loads_maybe(st.payload_json)
        status = str(payload_obj.get("status") or "").strip()
        active = 0 if status == "done" else 1
        if st.valid_to_ts is not None and int(st.valid_to_ts) < int(_now_utc_ts()) - 86400:
            active = 0

    if not text_in:
        return

    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_STATE_EMBEDDING)[0]
    item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_STATE), int(state_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(vector_index.VEC_KIND_STATE),
            rank_at=int(rank_at),
            active=int(active),
        )


def _handle_upsert_event_affect_embedding(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """event_affects の埋め込みを生成し vec_items へ upsert する。"""

    affect_id = int(payload.get("affect_id") or 0)
    if affect_id <= 0:
        raise RuntimeError("payload.affect_id is required")

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        aff = db.query(EventAffect).filter(EventAffect.id == int(affect_id)).one_or_none()
        if aff is None:
            return
        # --- 紐づく event が検索対象外なら、vec_items を消して終了 ---
        # NOTE:
        # - event_affect は event の派生であり、event が想起対象外なら affect も想起対象外とする。
        ev = db.query(Event).filter(Event.event_id == int(aff.event_id)).one_or_none()
        if ev is None or int(getattr(ev, "searchable", 1) or 0) != 1:
            item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT_AFFECT), int(affect_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_event_affect_embedding_text(aff)
        rank_at = int(aff.created_at)

    if not text_in:
        return

    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_EVENT_AFFECT_EMBEDDING)[0]
    item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT_AFFECT), int(affect_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(vector_index.VEC_KIND_EVENT_AFFECT),
            rank_at=int(rank_at),
            active=1,
        )


