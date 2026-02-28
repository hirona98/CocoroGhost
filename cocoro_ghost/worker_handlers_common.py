"""
Workerジョブハンドラ共通ヘルパ

役割:
- 各ジョブハンドラで共通利用する小物関数を提供する。
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import func

from cocoro_ghost import common_utils
from cocoro_ghost.storage.memory_models import Event, EventAffect, EventLink, EventThread, Job, State, StateLink
from cocoro_ghost.worker_constants import (
    JOB_DONE as _JOB_DONE,
    JOB_PENDING as _JOB_PENDING,
    JOB_RUNNING as _JOB_RUNNING,
    TIDY_MAX_CLOSE_PER_RUN as _TIDY_MAX_CLOSE_PER_RUN,
)


def _now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""

    return int(time.time())


def _normalize_text_for_dedupe(text_in: str) -> str:
    """重複判定用に本文テキストを正規化する（空白の揺れを吸収）。"""
    s = str(text_in or "").strip()
    if not s:
        return ""

    # --- 空白の連続を1つにする（日本語でも不都合が少ない） ---
    return " ".join(s.split())


def _canonicalize_json_for_dedupe(text_in: str) -> str:
    """重複判定用に payload_json を安定化する（JSONならキー順を揃える）。"""
    s = str(text_in or "").strip()
    if not s:
        return ""

    # --- JSONとして読めるなら、キー順を揃えてダンプする ---
    try:
        obj = json.loads(s)
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return s


def _has_pending_or_running_job(db, *, kind: str) -> bool:
    """指定kindの pending/running ジョブが存在するかを返す。"""
    n = (
        db.query(func.count(Job.id))
        .filter(Job.kind == str(kind))
        .filter(Job.status.in_([int(_JOB_PENDING), int(_JOB_RUNNING)]))
        .scalar()
    )
    return int(n or 0) > 0


def _last_tidy_watermark_event_id(db) -> int:
    """最後に完了した tidy_memory が対象にした event_id の上限（watermark）を返す。"""
    last = (
        db.query(Job)
        .filter(Job.kind == "tidy_memory")
        .filter(Job.status == int(_JOB_DONE))
        .order_by(Job.id.desc())
        .first()
    )
    if last is None:
        return 0

    payload = common_utils.json_loads_maybe(str(last.payload_json or ""))
    try:
        return int(payload.get("watermark_event_id") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _enqueue_tidy_memory_job(
    *,
    db,
    now_ts: int,
    run_after: int,
    reason: str,
    watermark_event_id: int,
) -> None:
    """記憶整理ジョブ（tidy_memory）を投入する（重複投入は呼び出し側で防ぐ）。"""
    db.add(
        Job(
            kind="tidy_memory",
            payload_json=common_utils.json_dumps(
                {
                    "reason": str(reason),
                    "max_close_per_run": int(_TIDY_MAX_CLOSE_PER_RUN),
                    "watermark_event_id": int(watermark_event_id),
                }
            ),
            status=int(_JOB_PENDING),
            run_after=int(run_after),
            tries=0,
            last_error=None,
            created_at=int(now_ts),
            updated_at=int(now_ts),
        )
    )


def _build_event_embedding_text(ev: Event) -> str:
    """埋め込み対象テキストを組み立てる。"""
    parts: list[str] = []

    # --- user_text ---
    ut = str(ev.user_text or "").strip()
    if ut:
        parts.append(ut)

    # --- assistant_text ---
    at = str(ev.assistant_text or "").strip()
    if at:
        if parts:
            parts.append("")
        parts.append(at)

    # --- 画像要約（内部用） ---
    # NOTE:
    # - 画像そのものは保存しないが、要約は検索に効かせるため埋め込みへ含める。
    img_json = str(getattr(ev, "image_summaries_json", None) or "").strip()
    if img_json:
        try:
            obj = json.loads(img_json)
        except Exception:  # noqa: BLE001
            obj = None
        if isinstance(obj, list):
            summaries = [str(x or "").strip() for x in obj if str(x or "").strip()]
            if summaries:
                if parts:
                    parts.append("")
                parts.append("[画像要約]")
                parts.extend(summaries)

    # --- source は短く補助情報として付ける ---
    parts.append("")
    parts.append(f"(source={str(ev.source)})")

    # --- 長すぎる場合は頭側を優先して切る ---
    text_out = "\n".join([p for p in parts if p is not None]).strip()
    if len(text_out) > 8000:
        text_out = text_out[:8000]
    return text_out


def _build_state_embedding_text(st: State) -> str:
    """状態の埋め込み対象テキストを組み立てる。"""
    parts: list[str] = []

    # --- kind ---
    parts.append(f"(kind={str(st.kind)})")

    # --- body_text ---
    bt = str(st.body_text or "").strip()
    if bt:
        parts.append(bt)

    # --- payload は補助情報として短く添える ---
    pj = str(st.payload_json or "").strip()
    if pj:
        parts.append(f"(payload={pj[:1200]})")

    text_out = "\n".join([p for p in parts if p is not None]).strip()
    if len(text_out) > 8000:
        text_out = text_out[:8000]
    return text_out


def _build_event_affect_embedding_text(aff: EventAffect) -> str:
    """イベント感情の埋め込み対象テキストを組み立てる。"""
    parts: list[str] = []

    # --- moment_affect_text ---
    t = common_utils.strip_face_tags(str(aff.moment_affect_text or "").strip())
    if t:
        parts.append(t)

    # --- moment_affect_labels ---
    labels = common_utils.parse_json_str_list(str(getattr(aff, "moment_affect_labels_json", "") or ""))
    if labels:
        parts.append("")
        parts.append(f"【ラベル】 {', '.join(labels[:6])}")

    # --- VAD ---
    parts.append("")
    parts.append(f"(vad v={float(aff.vad_v):.3f} a={float(aff.vad_a):.3f} d={float(aff.vad_d):.3f})")

    text_out = "\n".join([p for p in parts if p is not None]).strip()
    if len(text_out) > 8000:
        text_out = text_out[:8000]
    return text_out


def _build_event_assistant_summary_input(ev: Event) -> str:
    """要約生成のための入力テキストを組み立てる。

    方針:
        - 事実追加を防ぐため、材料は events の本文（user/assistant/画像要約）だけに限定する。
        - LLMが読みやすいようにラベルを付ける。
        - 長文は切り詰める（workerでのコスト暴れを防ぐ）。
    """

    parts: list[str] = []

    # --- user_text ---
    ut = str(ev.user_text or "").strip()
    if ut:
        parts.append("[user_text]")
        parts.append(ut)

    # --- assistant_text ---
    at = common_utils.strip_face_tags(str(ev.assistant_text or "").strip())
    if at:
        if parts:
            parts.append("")
        parts.append("[assistant_text]")
        parts.append(at)

    # --- 画像要約（内部用） ---
    img_json = str(getattr(ev, "image_summaries_json", None) or "").strip()
    if img_json:
        try:
            obj = json.loads(img_json)
        except Exception:  # noqa: BLE001
            obj = None
        if isinstance(obj, list):
            summaries = [str(x or "").strip() for x in obj if str(x or "").strip()]
            if summaries:
                if parts:
                    parts.append("")
                parts.append("[image_summaries]")
                parts.extend(summaries[:5])

    text_out = "\n".join([p for p in parts if p is not None]).strip()

    # --- 長すぎる場合は頭側を優先して切る（要約の目的は「何の話か」） ---
    if len(text_out) > 6000:
        text_out = text_out[:6000]
    return text_out


def _state_row_to_json(st: State) -> dict[str, Any]:
    """State行をJSON化する（LLM入力/Revision保存の両方で使う）。"""
    return {
        "state_id": int(st.state_id),
        "kind": str(st.kind),
        "body_text": str(st.body_text),
        "payload_json": str(st.payload_json),
        "last_confirmed_at": int(st.last_confirmed_at),
        "confidence": float(st.confidence),
        "valid_from_ts": (int(st.valid_from_ts) if st.valid_from_ts is not None else None),
        "valid_to_ts": (int(st.valid_to_ts) if st.valid_to_ts is not None else None),
        "created_at": int(st.created_at),
        "updated_at": int(st.updated_at),
    }


def _link_row_to_json(link: EventLink) -> dict[str, Any]:
    """EventLink行をJSON化する（Revision保存用）。"""
    return {
        "id": int(link.id),
        "from_event_id": int(link.from_event_id),
        "to_event_id": int(link.to_event_id),
        "label": str(link.label),
        "confidence": float(link.confidence),
        "evidence_event_ids_json": str(link.evidence_event_ids_json),
        "created_at": int(link.created_at),
    }


def _state_link_row_to_json(link: StateLink) -> dict[str, Any]:
    """StateLink行をJSON化する（Revision保存用）。"""
    return {
        "id": int(link.id),
        "from_state_id": int(link.from_state_id),
        "to_state_id": int(link.to_state_id),
        "label": str(link.label),
        "confidence": float(link.confidence),
        "evidence_event_ids_json": str(link.evidence_event_ids_json),
        "created_at": int(link.created_at),
    }


def _thread_row_to_json(th: EventThread) -> dict[str, Any]:
    """EventThread行をJSON化する（Revision保存用）。"""
    return {
        "id": int(th.id),
        "event_id": int(th.event_id),
        "thread_key": str(th.thread_key),
        "confidence": float(th.confidence),
        "evidence_event_ids_json": str(th.evidence_event_ids_json),
        "created_at": int(th.created_at),
    }


def _affect_row_to_json(aff: EventAffect) -> dict[str, Any]:
    """EventAffect行をJSON化する（Revision保存用）。"""
    return {
        "id": int(aff.id),
        "event_id": int(aff.event_id),
        "created_at": int(aff.created_at),
        "moment_affect_text": str(aff.moment_affect_text),
        "moment_affect_labels_json": str(aff.moment_affect_labels_json),
        "vad_v": float(aff.vad_v),
        "vad_a": float(aff.vad_a),
        "vad_d": float(aff.vad_d),
        "confidence": float(aff.confidence),
    }


