"""
気分（LongMoodState）デバッグAPI

提供する機能:
    - 現在の long_mood_state を、観測/デバッグ向けに取得する。
    - 直近の event_affects（瞬間感情）を、観測/デバッグ向けに取得する。

注意:
    - 運用前のため互換は付けない（仕様変更は許容）。
    - shock は「余韻」なので、読み出し時点で時間減衰させた値（shock_vad）を返す。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from cocoro_ghost import affect
from cocoro_ghost import common_utils
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.deps import get_config_store_dep
from cocoro_ghost.memory_models import Event, EventAffect, State
from cocoro_ghost.time_utils import format_iso8601_local


router = APIRouter(prefix="/mood", tags=["mood"])

# --- デバッグ用: 直近event_affectの件数（固定） ---
# NOTE:
# - クエリパラメータで増減させると互換や運用が複雑になるため、サーバ側で固定する。
# - まずは「直近の揺れ」を見る用途なので、少数（例: 8）で十分。
_RECENT_EVENT_AFFECTS_LIMIT = 8


@router.get("/debug")
def get_mood_debug(
    config_store: ConfigStore = Depends(get_config_store_dep),
) -> dict[str, Any]:
    """
    現在の「背景の気分（LongMoodState）」を、デバッグ観測向けに返す。

    仕様:
        - 認証: Bearer のみ（ルータ登録側で強制）
        - embedding_preset_id: サーバのアクティブ設定を使用
        - long_mood_state が無い場合: 200 + { "mood": null, "recent_affects": [...], "limits": {...} }
        - memory_enabled=false の場合: 503
        - shock_vad: now 時点で時間減衰した値を返す（dt_seconds を併記）
        - recent_affects: 直近の event_affects（全source）を返す（events.searchable=1 のみ）
    """

    # --- 記憶機能が無効なら 503 ---
    if not config_store.memory_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memory is disabled (memory_enabled=false)",
        )

    # --- 現在時刻（UTCのUNIX秒） ---
    now_ts = int(time.time())

    # --- 設定（アクティブDB） ---
    cfg = config_store.config
    embedding_preset_id = str(cfg.embedding_preset_id).strip()
    embedding_dimension = int(cfg.embedding_dimension)

    # --- 記憶DBを開く ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- 直近の瞬間感情（event_affects）を取る ---
        # NOTE:
        # - ここは「デバッグ表示」なので、本文の可読性と追跡可能性を優先する。
        # - events.searchable=0（誤想起の分離等）は混ぜない（ノイズを避ける）。
        recent_affects: list[dict[str, Any]] = []
        rows = (
            db.query(
                EventAffect.id,
                EventAffect.event_id,
                Event.source,
                Event.created_at,
                EventAffect.created_at,
                EventAffect.moment_affect_text,
                EventAffect.moment_affect_labels_json,
                EventAffect.vad_v,
                EventAffect.vad_a,
                EventAffect.vad_d,
                EventAffect.confidence,
            )
            .join(Event, Event.event_id == EventAffect.event_id)
            .filter(Event.searchable == 1)
            .order_by(EventAffect.created_at.desc(), EventAffect.id.desc())
            .limit(int(_RECENT_EVENT_AFFECTS_LIMIT))
            .all()
        )
        for r in rows:
            if not r:
                continue
            recent_affects.append(
                {
                    "affect_id": int(r[0] or 0),
                    "event_id": int(r[1] or 0),
                    "event_source": str(r[2] or "").strip(),
                    "event_created_at": format_iso8601_local(int(r[3] or 0)),
                    "affect_created_at": format_iso8601_local(int(r[4] or 0)),
                    "moment_affect_text": str(r[5] or ""),
                    "moment_affect_labels": common_utils.parse_json_str_list(str(r[6] or "")),
                    "vad": affect.vad_dict(float(r[7] or 0.0), float(r[8] or 0.0), float(r[9] or 0.0)),
                    "confidence": float(r[10] or 0.0),
                }
            )

        st = (
            db.query(State)
            .filter(State.kind == "long_mood_state")
            .filter(State.searchable == 1)
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .first()
        )

        # --- 未作成なら null ---
        if st is None:
            return {
                "mood": None,
                "recent_affects": list(recent_affects),
                "limits": {"recent_affects_limit": int(_RECENT_EVENT_AFFECTS_LIMIT)},
            }

        # --- payload_json を dict として扱う（壊れていても落とさない） ---
        payload_obj: Any = affect.parse_long_mood_payload(str(st.payload_json or ""))

        # --- payload から VAD を読む（無ければ 0.0 埋め） ---
        baseline_vad = affect.vad_dict(0.0, 0.0, 0.0)
        shock_vad = affect.vad_dict(0.0, 0.0, 0.0)
        if isinstance(payload_obj, dict):
            bv = affect.extract_vad_from_payload_obj(payload_obj, "baseline_vad")
            sv = affect.extract_vad_from_payload_obj(payload_obj, "shock_vad")
            if bv is not None:
                baseline_vad = dict(bv)
            if sv is not None:
                shock_vad = dict(sv)

        # --- dt_seconds（shock減衰に使用） ---
        try:
            dt_seconds = int(now_ts) - int(st.last_confirmed_at)
        except Exception:  # noqa: BLE001
            dt_seconds = 0
        if dt_seconds < 0:
            dt_seconds = 0

        # --- shock 半減期（payloadが無い/壊れている場合は既定） ---
        shock_halflife_seconds = 0
        if isinstance(payload_obj, dict):
            try:
                shock_halflife_seconds = int(payload_obj.get("shock_halflife_seconds") or 0)
            except Exception:  # noqa: BLE001
                shock_halflife_seconds = 0

        # --- shock を now 時点で減衰 ---
        shock_decayed = affect.decay_shock_for_snapshot(
            shock_vad=shock_vad,
            dt_seconds=int(dt_seconds),
            shock_halflife_seconds=int(shock_halflife_seconds),
        )

        # --- baseline + shock（減衰後） ---
        combined_vad = affect.vad_add(baseline_vad, shock_decayed)

        return {
            "mood": {
                "state_id": int(st.state_id),
                "body_text": str(st.body_text),
                "confidence": float(st.confidence),
                "salience": float(st.salience),
                "payload": payload_obj,
                "baseline_vad": baseline_vad,
                "shock_vad": shock_decayed,
                "vad": combined_vad,
                "now": format_iso8601_local(int(now_ts)),
                "dt_seconds": int(dt_seconds),
                "last_confirmed_at": format_iso8601_local(int(st.last_confirmed_at)),
            },
            "recent_affects": list(recent_affects),
            "limits": {"recent_affects_limit": int(_RECENT_EVENT_AFFECTS_LIMIT)},
        }
