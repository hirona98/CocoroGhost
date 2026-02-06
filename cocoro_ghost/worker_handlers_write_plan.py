"""
Workerジョブハンドラ（WritePlan系）

役割:
- WritePlan生成（generate_write_plan）
- WritePlan適用（apply_write_plan）
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func

from cocoro_ghost import affect
from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost import entity_utils
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import (
    Event,
    EventAffect,
    EventEntity,
    EventLink,
    EventThread,
    Job,
    Revision,
    State,
    StateEntity,
    UserPreference,
)
from cocoro_ghost.time_utils import format_iso8601_local, parse_iso8601_to_utc_ts
from cocoro_ghost.worker_constants import (
    JOB_PENDING as _JOB_PENDING,
    TIDY_CHAT_TURNS_INTERVAL as _TIDY_CHAT_TURNS_INTERVAL,
    TIDY_CHAT_TURNS_INTERVAL_FIRST as _TIDY_CHAT_TURNS_INTERVAL_FIRST,
)
from cocoro_ghost.worker_handlers_common import (
    _affect_row_to_json,
    _enqueue_tidy_memory_job,
    _has_pending_or_running_job,
    _last_tidy_watermark_event_id,
    _link_row_to_json,
    _now_utc_ts,
    _state_row_to_json,
    _thread_row_to_json,
)


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


def _handle_apply_write_plan(*, embedding_preset_id: str, embedding_dimension: int, payload: dict[str, Any]) -> None:
    """WritePlan を適用し、状態/感情/文脈グラフを更新する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    plan = payload.get("write_plan")
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")
    if not isinstance(plan, dict):
        raise RuntimeError("payload.write_plan is required")

    now_ts = _now_utc_ts()

    # --- まとめて1トランザクションで適用する（途中失敗で半端に残さない） ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- LongMoodState 用に、今回の event_affect をメモしておく ---
        # NOTE:
        # - long_mood_state は「基調（baseline）」と「余韻（shock）」を分けて育てる。
        # - event_affect（瞬間反応）を入力として、shock を即時反映し、baseline は日スケールでゆっくり追従させる。
        moment_vad: dict[str, float] | None = None
        moment_conf: float = 0.0
        moment_note: str | None = None
        baseline_text_candidate: str | None = None

        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        base_ts = int(ev.created_at)

        # --- entity索引 更新ヘルパ（events/state） ---
        # NOTE:
        # - `events.entities_json` は監査/表示向けのスナップショットとして残す。
        # - 検索は `event_entities/state_entities` の正規化キー（type + name_norm）を正にする。
        def replace_event_entities(*, event_id_in: int, entities_norm_in: list[dict[str, Any]]) -> None:
            """event_entities を event_id 単位で作り直す（delete→insert）。"""

            # --- 既存を削除 ---
            db.query(EventEntity).filter(EventEntity.event_id == int(event_id_in)).delete(synchronize_session=False)

            # --- 新規を追加 ---
            for ent in entities_norm_in:
                t_norm = str(ent.get("type") or "").strip()
                name_raw = str(ent.get("name") or "").strip()
                if not t_norm or not name_raw:
                    continue
                name_norm = entity_utils.normalize_entity_name(name_raw)
                if not name_norm:
                    continue
                db.add(
                    EventEntity(
                        event_id=int(event_id_in),
                        entity_type_norm=str(t_norm),
                        entity_name_raw=str(name_raw),
                        entity_name_norm=str(name_norm),
                        confidence=float(entity_utils.clamp_01(ent.get("confidence"))),
                        created_at=int(base_ts),
                    )
                )

        def replace_state_entities(*, state_id_in: int, entities_norm_in: list[dict[str, Any]]) -> None:
            """state_entities を state_id 単位で作り直す（delete→insert）。"""

            # --- 既存を削除 ---
            db.query(StateEntity).filter(StateEntity.state_id == int(state_id_in)).delete(synchronize_session=False)

            # --- 新規を追加 ---
            for ent in entities_norm_in:
                t_norm = str(ent.get("type") or "").strip()
                name_raw = str(ent.get("name") or "").strip()
                if not t_norm or not name_raw:
                    continue
                name_norm = entity_utils.normalize_entity_name(name_raw)
                if not name_norm:
                    continue
                db.add(
                    StateEntity(
                        state_id=int(state_id_in),
                        entity_type_norm=str(t_norm),
                        entity_name_raw=str(name_raw),
                        entity_name_norm=str(name_norm),
                        confidence=float(entity_utils.clamp_01(ent.get("confidence"))),
                        created_at=int(base_ts),
                    )
                )

        # --- events 注釈更新（about_time/entities） ---
        ann = plan.get("event_annotations") if isinstance(plan, dict) else None
        entities_norm: list[dict[str, Any]] = []
        if isinstance(ann, dict):
            # NOTE: LLMはISO文字列を返す可能性があるため、ここでUNIX秒へ正規化する。
            a0 = ann.get("about_start_ts")
            a1 = ann.get("about_end_ts")
            a0_i = int(a0) if isinstance(a0, (int, float)) and int(a0) > 0 else parse_iso8601_to_utc_ts(str(a0) if a0 is not None else None)
            a1_i = int(a1) if isinstance(a1, (int, float)) and int(a1) > 0 else parse_iso8601_to_utc_ts(str(a1) if a1 is not None else None)
            ev.about_start_ts = int(a0_i) if a0_i is not None and int(a0_i) > 0 else None
            ev.about_end_ts = int(a1_i) if a1_i is not None and int(a1_i) > 0 else None

            y0 = ann.get("about_year_start")
            y1 = ann.get("about_year_end")
            ev.about_year_start = int(y0) if isinstance(y0, (int, float)) and int(y0) > 0 else None
            ev.about_year_end = int(y1) if isinstance(y1, (int, float)) and int(y1) > 0 else None
            life_stage = ann.get("life_stage")
            ev.life_stage = (str(life_stage) if life_stage is not None else None)
            ev.about_time_confidence = float(ann.get("about_time_confidence") or 0.0)
            # --- entities を正規化し、イベントへ保存 + 索引へ落とす ---
            entities_raw = ann.get("entities") if isinstance(ann.get("entities"), list) else []
            entities_norm = entity_utils.normalize_entities(entities_raw)
            ev.entities_json = common_utils.json_dumps(entities_norm)
            replace_event_entities(event_id_in=int(event_id), entities_norm_in=list(entities_norm))
        ev.updated_at = int(now_ts)
        db.add(ev)

        # --- revisions を追加するヘルパ ---
        def add_revision(*, entity_type: str, entity_id: int, before: Any, after: Any, reason: str, evidence_event_ids: list[int]) -> None:
            db.add(
                Revision(
                    entity_type=str(entity_type),
                    entity_id=int(entity_id),
                    before_json=(common_utils.json_dumps(before) if before is not None else None),
                    after_json=(common_utils.json_dumps(after) if after is not None else None),
                    reason=str(reason or "").strip() or "(no reason)",
                    evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_event_ids if int(x) > 0]),
                    created_at=int(now_ts),
                )
            )

        # --- user_preferences（好み/苦手: confirmed/candidate） ---
        # NOTE:
        # - ここは「ユーザーの性格/習慣」ではなく、「好き/苦手」を保存する用途に限定する。
        # - 断定してよい根拠は status=confirmed のみとし、会話生成側で参照する。
        pref_updates = plan.get("preference_updates") if isinstance(plan, dict) else None
        pref_updates = pref_updates if isinstance(pref_updates, list) else []

        def _normalize_pref_domain(domain_in: Any) -> str | None:
            """domain を food/topic/style のいずれかへ正規化する。"""
            s = str(domain_in or "").strip().lower()
            if s in ("food", "topic", "style"):
                return s
            return None

        def _normalize_pref_polarity(polarity_in: Any) -> str | None:
            """polarity を like/dislike のいずれかへ正規化する。"""
            s = str(polarity_in or "").strip().lower()
            if s in ("like", "dislike"):
                return s
            return None

        def _normalize_pref_status_op(op_in: Any) -> str | None:
            """op を upsert_candidate/confirm/revoke のいずれかへ正規化する。"""
            s = str(op_in or "").strip().lower()
            if s in ("upsert_candidate", "confirm", "revoke"):
                return s
            return None

        def _merge_evidence_event_ids(*, prev_json: str, add_ids: list[int]) -> str:
            """evidence_event_ids_json を「重複除去＋上限」で安定化して返す。"""
            # --- 既存を読む（壊れていても落とさない） ---
            prev = common_utils.json_loads_maybe(str(prev_json or ""))
            prev_ids = prev if isinstance(prev, list) else []
            merged: list[int] = []
            seen: set[int] = set()

            # --- 既存→追加の順で安定化（新しい根拠を後ろに足す） ---
            for x in list(prev_ids) + list(add_ids or []):
                try:
                    i = int(x or 0)
                except Exception:  # noqa: BLE001
                    continue
                if i <= 0:
                    continue
                if i in seen:
                    continue
                seen.add(i)
                merged.append(i)

            # --- 上限（肥大化防止。監査性は revisions で担保する） ---
            if len(merged) > 20:
                merged = merged[-20:]
            return common_utils.json_dumps(merged)

        def _user_preference_row_to_json(row: UserPreference) -> dict[str, Any]:
            """UserPreference を監査用 JSON へ変換する。"""
            return {
                "id": int(row.id),
                "domain": str(row.domain),
                "polarity": str(row.polarity),
                "subject_raw": str(row.subject_raw),
                "subject_norm": str(row.subject_norm),
                "status": str(row.status),
                "confidence": float(row.confidence),
                "note": (str(row.note) if row.note is not None else None),
                "evidence_event_ids_json": str(row.evidence_event_ids_json or "[]"),
                "first_seen_at": int(row.first_seen_at),
                "last_seen_at": int(row.last_seen_at),
                "confirmed_at": (int(row.confirmed_at) if row.confirmed_at is not None else None),
                "revoked_at": (int(row.revoked_at) if row.revoked_at is not None else None),
                "created_at": int(row.created_at),
                "updated_at": int(row.updated_at),
            }

        for u in pref_updates:
            if not isinstance(u, dict):
                continue

            # --- op/domain/polarity/subject を正規化 ---
            op = _normalize_pref_status_op(u.get("op"))
            domain = _normalize_pref_domain(u.get("domain"))
            polarity = _normalize_pref_polarity(u.get("polarity"))
            subject_raw = str(u.get("subject") or "").strip()
            reason = str(u.get("reason") or "").strip() or "preference を更新した"

            if op is None or domain is None or polarity is None or not subject_raw:
                continue

            # --- subject_norm は比較キー。表記ゆれを吸収する ---
            subject_norm = entity_utils.normalize_entity_name(subject_raw, max_len=80)
            if not subject_norm:
                continue

            # --- confidence/note/evidence ---
            confidence = float(entity_utils.clamp_01(u.get("confidence")))
            note = str(u.get("note") or "").strip()
            if note:
                note = note[:240]
            else:
                note = ""

            evidence_ids = u.get("evidence_event_ids") if isinstance(u.get("evidence_event_ids"), list) else []
            evidence_ids_norm = [int(x) for x in evidence_ids if isinstance(x, (int, float)) and int(x) > 0]
            # NOTE: どの更新も現在の event_id を含める前提。欠けていてもここで補正する。
            if int(event_id) not in set(evidence_ids_norm):
                evidence_ids_norm.append(int(event_id))

            # --- 対象行を取得（1行=現在状態。履歴は revisions） ---
            row = (
                db.query(UserPreference)
                .filter(UserPreference.domain == str(domain))
                .filter(UserPreference.subject_norm == str(subject_norm))
                .filter(UserPreference.polarity == str(polarity))
                .one_or_none()
            )

            # --- 変更前スナップショット ---
            before = _user_preference_row_to_json(row) if row is not None else None

            # --- opごとの適用 ---
            if op == "upsert_candidate":
                if row is None:
                    row = UserPreference(
                        domain=str(domain),
                        polarity=str(polarity),
                        subject_raw=str(subject_raw)[:120],
                        subject_norm=str(subject_norm),
                        status="candidate",
                        confidence=float(confidence),
                        note=(note if note else None),
                        evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_ids_norm if int(x) > 0]),
                        first_seen_at=int(base_ts),
                        last_seen_at=int(base_ts),
                        confirmed_at=None,
                        revoked_at=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                    db.add(row)
                    db.flush()
                else:
                    # NOTE:
                    # - confirmed は「断定して良い」ため、candidate で上書きしない。
                    # - revoked/candidate は、候補として再浮上させることがあるため candidate へ戻してよい。
                    if str(row.status) != "confirmed":
                        row.status = "candidate"
                        row.revoked_at = None
                        row.confirmed_at = None
                    row.subject_raw = str(subject_raw)[:120]
                    row.last_seen_at = int(base_ts)
                    row.confidence = float(confidence)
                    row.note = (note if note else None)
                    row.evidence_event_ids_json = _merge_evidence_event_ids(
                        prev_json=str(row.evidence_event_ids_json or "[]"),
                        add_ids=list(evidence_ids_norm),
                    )
                    row.updated_at = int(now_ts)
                    db.add(row)
                    db.flush()

                add_revision(
                    entity_type="user_preferences",
                    entity_id=int(row.id),
                    before=before,
                    after=_user_preference_row_to_json(row),
                    reason=str(reason),
                    evidence_event_ids=list(evidence_ids_norm),
                )
                continue

            if op == "confirm":
                if row is None:
                    row = UserPreference(
                        domain=str(domain),
                        polarity=str(polarity),
                        subject_raw=str(subject_raw)[:120],
                        subject_norm=str(subject_norm),
                        status="confirmed",
                        confidence=float(confidence),
                        note=(note if note else None),
                        evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_ids_norm if int(x) > 0]),
                        first_seen_at=int(base_ts),
                        last_seen_at=int(base_ts),
                        confirmed_at=int(base_ts),
                        revoked_at=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                    db.add(row)
                    db.flush()
                else:
                    row.status = "confirmed"
                    row.subject_raw = str(subject_raw)[:120]
                    row.last_seen_at = int(base_ts)
                    row.confidence = float(confidence)
                    row.note = (note if note else None)
                    row.evidence_event_ids_json = _merge_evidence_event_ids(
                        prev_json=str(row.evidence_event_ids_json or "[]"),
                        add_ids=list(evidence_ids_norm),
                    )
                    row.confirmed_at = int(base_ts)
                    row.revoked_at = None
                    row.updated_at = int(now_ts)
                    db.add(row)
                    db.flush()

                add_revision(
                    entity_type="user_preferences",
                    entity_id=int(row.id),
                    before=before,
                    after=_user_preference_row_to_json(row),
                    reason=str(reason),
                    evidence_event_ids=list(evidence_ids_norm),
                )

                # --- 矛盾の自動revoke（同一 subject の反対極性 confirmed を無効化する） ---
                opposite = "dislike" if str(polarity) == "like" else "like"
                opp = (
                    db.query(UserPreference)
                    .filter(UserPreference.domain == str(domain))
                    .filter(UserPreference.subject_norm == str(subject_norm))
                    .filter(UserPreference.polarity == str(opposite))
                    .one_or_none()
                )
                if opp is not None and str(opp.status) == "confirmed":
                    opp_before = _user_preference_row_to_json(opp)
                    opp.status = "revoked"
                    opp.revoked_at = int(base_ts)
                    opp.last_seen_at = int(base_ts)
                    opp.updated_at = int(now_ts)
                    opp.evidence_event_ids_json = _merge_evidence_event_ids(
                        prev_json=str(opp.evidence_event_ids_json or "[]"),
                        add_ids=list(evidence_ids_norm),
                    )
                    db.add(opp)
                    db.flush()
                    add_revision(
                        entity_type="user_preferences",
                        entity_id=int(opp.id),
                        before=opp_before,
                        after=_user_preference_row_to_json(opp),
                        reason="矛盾する好みを自動revokeした",
                        evidence_event_ids=list(evidence_ids_norm),
                    )
                continue

            if op == "revoke":
                if row is None:
                    continue
                if str(row.status) == "revoked":
                    continue
                row_before = _user_preference_row_to_json(row)
                row.status = "revoked"
                row.revoked_at = int(base_ts)
                row.last_seen_at = int(base_ts)
                row.updated_at = int(now_ts)
                row.evidence_event_ids_json = _merge_evidence_event_ids(
                    prev_json=str(row.evidence_event_ids_json or "[]"),
                    add_ids=list(evidence_ids_norm),
                )
                db.add(row)
                db.flush()
                add_revision(
                    entity_type="user_preferences",
                    entity_id=int(row.id),
                    before=row_before,
                    after=_user_preference_row_to_json(row),
                    reason=str(reason),
                    evidence_event_ids=list(evidence_ids_norm),
                )
                continue

        # --- event_affect（瞬間的な感情） ---
        ea = plan.get("event_affect") if isinstance(plan, dict) else None
        if isinstance(ea, dict):
            moment_text = common_utils.strip_face_tags(str(ea.get("moment_affect_text") or "").strip())
            if moment_text:
                score = ea.get("moment_affect_score_vad") if isinstance(ea.get("moment_affect_score_vad"), dict) else {}
                conf = float(ea.get("moment_affect_confidence") or 0.0)
                labels = affect.sanitize_moment_affect_labels(ea.get("moment_affect_labels"))

                # --- LongMoodState 更新用のメモ（保存の成否に依存しない入力） ---
                moment_vad = affect.vad_dict(score.get("v"), score.get("a"), score.get("d"))
                moment_conf = affect.clamp_01(conf)
                moment_note = str(moment_text)[:240]

                # NOTE: 1 event につき1件として扱い、既存があれば更新する（重複を避ける）
                existing = (
                    db.query(EventAffect)
                    .filter(EventAffect.event_id == int(event_id))
                    .order_by(EventAffect.id.desc())
                    .first()
                )
                if existing is None:
                    aff = EventAffect(
                        event_id=int(event_id),
                        created_at=int(base_ts),
                        moment_affect_text=str(moment_text),
                        moment_affect_labels_json=common_utils.json_dumps(labels),
                        vad_v=affect.clamp_vad(score.get("v")),
                        vad_a=affect.clamp_vad(score.get("a")),
                        vad_d=affect.clamp_vad(score.get("d")),
                        confidence=float(conf),
                    )
                    db.add(aff)
                    db.flush()
                    add_revision(
                        entity_type="event_affects",
                        entity_id=int(aff.id),
                        before=None,
                        after=_affect_row_to_json(aff),
                        reason="event_affect を保存した",
                        evidence_event_ids=[int(event_id)],
                    )
                    affect_id = int(aff.id)
                else:
                    before = _affect_row_to_json(existing)
                    existing.moment_affect_text = str(moment_text)
                    existing.moment_affect_labels_json = common_utils.json_dumps(labels)
                    existing.vad_v = affect.clamp_vad(score.get("v"))
                    existing.vad_a = affect.clamp_vad(score.get("a"))
                    existing.vad_d = affect.clamp_vad(score.get("d"))
                    existing.confidence = float(conf)
                    db.add(existing)
                    db.flush()
                    add_revision(
                        entity_type="event_affects",
                        entity_id=int(existing.id),
                        before=before,
                        after=_affect_row_to_json(existing),
                        reason="event_affect を更新した",
                        evidence_event_ids=[int(event_id)],
                    )
                    affect_id = int(existing.id)

                # --- event_affect embedding job ---
                db.add(
                    Job(
                        kind="upsert_event_affect_embedding",
                        payload_json=common_utils.json_dumps({"affect_id": int(affect_id)}),
                        status=int(_JOB_PENDING),
                        run_after=int(now_ts),
                        tries=0,
                        last_error=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                )

        # --- 文脈グラフ（threads/links） ---
        cu = plan.get("context_updates") if isinstance(plan, dict) else None
        if isinstance(cu, dict):
            threads = cu.get("threads") if isinstance(cu.get("threads"), list) else []
            links = cu.get("links") if isinstance(cu.get("links"), list) else []

            # --- threads ---
            for t in threads:
                if not isinstance(t, dict):
                    continue
                thread_key = str(t.get("thread_key") or "").strip()
                if not thread_key:
                    continue
                confidence = float(t.get("confidence") or 0.0)

                existing = (
                    db.query(EventThread)
                    .filter(EventThread.event_id == int(event_id))
                    .filter(EventThread.thread_key == str(thread_key))
                    .one_or_none()
                )
                if existing is None:
                    th = EventThread(
                        event_id=int(event_id),
                        thread_key=str(thread_key),
                        confidence=float(confidence),
                        evidence_event_ids_json=common_utils.json_dumps([int(event_id)]),
                        created_at=int(now_ts),
                    )
                    db.add(th)
                    db.flush()
                    add_revision(
                        entity_type="event_threads",
                        entity_id=int(th.id),
                        before=None,
                        after=_thread_row_to_json(th),
                        reason="文脈スレッドを付与した",
                        evidence_event_ids=[int(event_id)],
                    )
                else:
                    before = _thread_row_to_json(existing)
                    existing.confidence = float(confidence)
                    existing.evidence_event_ids_json = common_utils.json_dumps([int(event_id)])
                    db.add(existing)
                    db.flush()
                    add_revision(
                        entity_type="event_threads",
                        entity_id=int(existing.id),
                        before=before,
                        after=_thread_row_to_json(existing),
                        reason="文脈スレッドの信頼度を更新した",
                        evidence_event_ids=[int(event_id)],
                    )

            # --- links ---
            for l in links:
                if not isinstance(l, dict):
                    continue
                to_event_id = int(l.get("to_event_id") or 0)
                label = str(l.get("label") or "").strip()
                if to_event_id <= 0 or not label:
                    continue
                confidence = float(l.get("confidence") or 0.0)

                existing = (
                    db.query(EventLink)
                    .filter(EventLink.from_event_id == int(event_id))
                    .filter(EventLink.to_event_id == int(to_event_id))
                    .filter(EventLink.label == str(label))
                    .one_or_none()
                )
                if existing is None:
                    link = EventLink(
                        from_event_id=int(event_id),
                        to_event_id=int(to_event_id),
                        label=str(label),
                        confidence=float(confidence),
                        evidence_event_ids_json=common_utils.json_dumps([int(event_id)]),
                        created_at=int(now_ts),
                    )
                    db.add(link)
                    db.flush()
                    add_revision(
                        entity_type="event_links",
                        entity_id=int(link.id),
                        before=None,
                        after=_link_row_to_json(link),
                        reason="文脈リンクを追加した",
                        evidence_event_ids=[int(event_id)],
                    )
                else:
                    before = _link_row_to_json(existing)
                    existing.confidence = float(confidence)
                    existing.evidence_event_ids_json = common_utils.json_dumps([int(event_id)])
                    db.add(existing)
                    db.flush()
                    add_revision(
                        entity_type="event_links",
                        entity_id=int(existing.id),
                        before=before,
                        after=_link_row_to_json(existing),
                        reason="文脈リンクの信頼度を更新した",
                        evidence_event_ids=[int(event_id)],
                    )

        # --- state 更新 ---
        # NOTE:
        # - state_links（B-1）のリンク生成は非同期ジョブで行う。
        # - ここでは「今回更新したstate_id」を集めて、後段で build_state_links を投入する。
        updated_state_ids_for_links: set[int] = set()
        su = plan.get("state_updates") if isinstance(plan, dict) else None
        if isinstance(su, list):
            for u in su:
                if not isinstance(u, dict):
                    continue

                # --- state_entities 用の entities（state単位） ---
                entities_norm_for_state = entity_utils.normalize_entities(u.get("entities"))

                op = str(u.get("op") or "").strip()
                kind = str(u.get("kind") or "").strip()
                reason = str(u.get("reason") or "").strip() or "(no reason)"
                evidence = u.get("evidence_event_ids") if isinstance(u.get("evidence_event_ids"), list) else [int(event_id)]
                evidence_ids = [int(x) for x in evidence if int(x) > 0]
                if int(event_id) not in evidence_ids:
                    evidence_ids.append(int(event_id))

                state_id = u.get("state_id")
                state_id_i = int(state_id) if isinstance(state_id, (int, float)) and int(state_id) > 0 else None

                # --- last_confirmed_at は event 時刻を正とする（LLMが時刻を作ると破綻しやすい） ---
                # NOTE: 会話の時系列と最近性（検索順位）を壊さないため、常に base_ts を使う。
                last_confirmed_at_i = int(base_ts)

                valid_from_ts = u.get("valid_from_ts")
                valid_to_ts = u.get("valid_to_ts")
                # NOTE: LLMはISO文字列を返す可能性があるため、ここでUNIX秒へ正規化する。
                v_from = (
                    int(valid_from_ts)
                    if isinstance(valid_from_ts, (int, float)) and int(valid_from_ts) > 0
                    else parse_iso8601_to_utc_ts(str(valid_from_ts) if valid_from_ts is not None else None)
                )
                v_to = (
                    int(valid_to_ts)
                    if isinstance(valid_to_ts, (int, float)) and int(valid_to_ts) > 0
                    else parse_iso8601_to_utc_ts(str(valid_to_ts) if valid_to_ts is not None else None)
                )

                confidence = float(u.get("confidence") or 0.0)
                body_text = str(u.get("body_text") or "").strip()
                payload_obj = u.get("payload") if isinstance(u.get("payload"), dict) else {}

                # --- kind/op の最低限チェック ---
                if not op or not kind:
                    continue

                # --- close/mark_done は state_id 必須 ---
                if op in ("close", "mark_done") and state_id_i is None:
                    continue

                if op == "upsert":
                    # --- long_mood_state は「本文案」だけ拾う（数値はサーバ側で安定化する） ---
                    # NOTE:
                    # - long_mood_state を LLM の本文で毎ターン差し替えると、短期すぎる背景になりやすい。
                    # - baseline/shock の数値モデルはサーバで決め、本文（baseline_text）は日スケールで更新する。
                    if str(kind) == "long_mood_state":
                        if body_text:
                            baseline_text_candidate = str(body_text)
                        # NOTE: ここでは upsert を続行せず、後段の「LongMoodState 統合更新」で扱う。
                        continue

                    # --- 既存を更新するか、新規を作る ---
                    existing = db.query(State).filter(State.state_id == int(state_id_i)).one_or_none() if state_id_i else None

                    # --- long_mood_state は「単一更新」で育てる ---
                    # NOTE:
                    # - long_mood_state は「背景」として扱い、複数を並存させない（増殖させない）
                    # - 整理（tidy_memory）では long_mood_state を触らない前提なので、ここで増殖を防ぐ
                    if str(kind) == "long_mood_state":
                        if existing is not None and str(existing.kind) != "long_mood_state":
                            existing = None

                        if existing is None:
                            existing = (
                                db.query(State)
                                .filter(State.kind == "long_mood_state")
                                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                                .first()
                            )

                    if existing is None:
                        if not body_text:
                            body_text = common_utils.json_dumps(payload_obj)[:2000]
                        st = State(
                            kind=str(kind),
                            body_text=str(body_text),
                            payload_json=common_utils.json_dumps(payload_obj),
                            last_confirmed_at=int(last_confirmed_at_i),
                            confidence=float(confidence),
                            valid_from_ts=v_from,
                            valid_to_ts=v_to,
                            created_at=int(base_ts),
                            updated_at=int(now_ts),
                        )
                        db.add(st)
                        db.flush()
                        add_revision(
                            entity_type="state",
                            entity_id=int(st.state_id),
                            before=None,
                            after=_state_row_to_json(st),
                            reason=str(reason),
                            evidence_event_ids=evidence_ids,
                        )
                        changed_state_id = int(st.state_id)
                    else:
                        before = _state_row_to_json(existing)
                        existing.kind = str(kind)
                        if body_text:
                            existing.body_text = str(body_text)
                        existing.payload_json = common_utils.json_dumps(payload_obj)
                        existing.last_confirmed_at = int(last_confirmed_at_i)
                        existing.confidence = float(confidence)
                        existing.valid_from_ts = v_from
                        existing.valid_to_ts = v_to
                        existing.updated_at = int(now_ts)
                        db.add(existing)
                        db.flush()
                        add_revision(
                            entity_type="state",
                            entity_id=int(existing.state_id),
                            before=before,
                            after=_state_row_to_json(existing),
                            reason=str(reason),
                            evidence_event_ids=evidence_ids,
                        )
                        changed_state_id = int(existing.state_id)

                    # --- state entity索引（後で一括で付与） ---
                    if int(changed_state_id) > 0:
                        # --- state単位のentitiesを付与（delete→insert） ---
                        replace_state_entities(
                            state_id_in=int(changed_state_id),
                            entities_norm_in=list(entities_norm_for_state),
                        )
                        updated_state_ids_for_links.add(int(changed_state_id))

                    # --- state embedding job ---
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=common_utils.json_dumps({"state_id": int(changed_state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_ts),
                            updated_at=int(now_ts),
                        )
                    )

                elif op == "close":
                    st = db.query(State).filter(State.state_id == int(state_id_i)).one_or_none()
                    if st is None:
                        continue
                    before = _state_row_to_json(st)
                    # NOTE: valid_to が無い場合は event の about_end → 無ければ event時刻
                    st.valid_to_ts = v_to if v_to is not None else (int(ev.about_end_ts) if ev.about_end_ts is not None else int(base_ts))
                    st.last_confirmed_at = int(last_confirmed_at_i)
                    st.updated_at = int(now_ts)
                    db.add(st)
                    db.flush()
                    add_revision(
                        entity_type="state",
                        entity_id=int(st.state_id),
                        before=before,
                        after=_state_row_to_json(st),
                        reason=str(reason),
                        evidence_event_ids=evidence_ids,
                    )
                    # --- state entity索引（後で一括で付与） ---
                    if str(st.kind) != "long_mood_state":
                        replace_state_entities(
                            state_id_in=int(st.state_id),
                            entities_norm_in=list(entities_norm_for_state),
                        )
                        updated_state_ids_for_links.add(int(st.state_id))
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=common_utils.json_dumps({"state_id": int(st.state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_ts),
                            updated_at=int(now_ts),
                        )
                    )

                elif op == "mark_done":
                    st = db.query(State).filter(State.state_id == int(state_id_i)).one_or_none()
                    if st is None:
                        continue
                    before = _state_row_to_json(st)
                    # --- payload の status を done にする ---
                    pj = common_utils.json_loads_maybe(st.payload_json)
                    pj["status"] = "done"
                    st.payload_json = common_utils.json_dumps(pj)
                    st.last_confirmed_at = int(last_confirmed_at_i)
                    st.updated_at = int(now_ts)
                    db.add(st)
                    db.flush()
                    add_revision(
                        entity_type="state",
                        entity_id=int(st.state_id),
                        before=before,
                        after=_state_row_to_json(st),
                        reason=str(reason),
                        evidence_event_ids=evidence_ids,
                    )
                    # --- state entity索引（後で一括で付与） ---
                    if str(st.kind) != "long_mood_state":
                        replace_state_entities(
                            state_id_in=int(st.state_id),
                            entities_norm_in=list(entities_norm_for_state),
                        )
                        updated_state_ids_for_links.add(int(st.state_id))
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=common_utils.json_dumps({"state_id": int(st.state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_ts),
                            updated_at=int(now_ts),
                        )
                    )

        # --- LongMoodState の統合更新（baseline + shock） ---
        # NOTE:
        # - WritePlan の state_updates.long_mood_state は「本文案」として扱う。
        # - 数値（baseline/shock）は event_affect の moment_vad を入力として更新する。
        # - event_affect が無いターンでも、日付が変わった等で本文案があれば本文だけ反映できるようにする。
        if (moment_vad is not None and moment_conf > 0.0) or (baseline_text_candidate is not None and str(baseline_text_candidate).strip()):
            # --- 既存の long_mood_state を読む（単一前提） ---
            st = (
                db.query(State)
                .filter(State.kind == "long_mood_state")
                .filter(State.searchable == 1)
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .first()
            )
            # --- 共通ロジックで baseline+shock を更新する ---
            payload_prev_obj = affect.parse_long_mood_payload(str(st.payload_json or "")) if st is not None else None
            prev_body_text = str(st.body_text or "") if st is not None else None
            prev_confidence = float(st.confidence) if st is not None else None
            prev_last_confirmed_at = int(st.last_confirmed_at) if st is not None else None

            update_result = affect.update_long_mood(
                prev_state_exists=(st is not None),
                prev_payload_obj=payload_prev_obj,
                prev_body_text=prev_body_text,
                prev_confidence=prev_confidence,
                prev_last_confirmed_at=prev_last_confirmed_at,
                event_ts=int(base_ts),
                moment_vad=moment_vad,
                moment_confidence=float(moment_conf),
                moment_note=moment_note,
                baseline_text_candidate=baseline_text_candidate,
            )

            payload_new_json = common_utils.json_dumps(update_result.payload_obj)

            # --- 変更が無い場合は何もしない（revisionノイズを避ける） ---
            skip_upsert = False
            if st is not None:
                payload_prev_json = str(st.payload_json or "").strip()
                same_payload = payload_prev_json == payload_new_json
                same_text = str(st.body_text or "").strip() == str(update_result.body_text or "").strip()
                same_conf = abs(float(st.confidence) - float(update_result.confidence)) < 1e-9
                same_lca = int(st.last_confirmed_at) == int(update_result.last_confirmed_at)
                if same_payload and same_text and same_conf and same_lca:
                    skip_upsert = True

            # --- upsert（単一更新） ---
            if skip_upsert:
                changed_state_id = 0
            elif st is None:
                st2 = State(
                    kind="long_mood_state",
                    body_text=str(update_result.body_text),
                    payload_json=payload_new_json,
                    last_confirmed_at=int(update_result.last_confirmed_at),
                    confidence=float(update_result.confidence),
                    valid_from_ts=None,
                    valid_to_ts=None,
                    created_at=int(base_ts),
                    updated_at=int(now_ts),
                )
                db.add(st2)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(st2.state_id),
                    before=None,
                    after=_state_row_to_json(st2),
                    reason="long_mood_state を更新した（baseline+shock）",
                    evidence_event_ids=[int(event_id)],
                )
                changed_state_id = int(st2.state_id)
            else:
                before = _state_row_to_json(st)
                st.kind = "long_mood_state"
                st.body_text = str(update_result.body_text)
                st.payload_json = payload_new_json
                st.last_confirmed_at = int(update_result.last_confirmed_at)
                st.confidence = float(update_result.confidence)
                st.valid_from_ts = None
                st.valid_to_ts = None
                st.updated_at = int(now_ts)
                db.add(st)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(st.state_id),
                    before=before,
                    after=_state_row_to_json(st),
                    reason="long_mood_state を更新した（baseline+shock）",
                    evidence_event_ids=[int(event_id)],
                )
                changed_state_id = int(st.state_id)

            # --- embedding job（long_mood_state も更新で育つので反映しておく） ---
            if int(changed_state_id) > 0:
                db.add(
                    Job(
                        kind="upsert_state_embedding",
                        payload_json=common_utils.json_dumps({"state_id": int(changed_state_id)}),
                        status=int(_JOB_PENDING),
                        run_after=int(now_ts),
                        tries=0,
                        last_error=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                )

        # NOTE:
        # - state_entities は各 state_update の entities を正にして即時反映している。
        # - ここでは何もしない（ループ内で確定した state_id に対して更新済み）。

        # --- state_links リンク生成ジョブ ---
        # NOTE:
        # - state↔state のリンク生成は「同期検索の前」には重すぎるため、非同期ジョブとして回す。
        # - apply_write_plan は「記憶更新の本体」なので、リンク生成の失敗で全体を止めない（try/exceptで握る）。
        # - long_mood_state は対象外（背景であり、リンクで増殖させない）。
        try:
            from cocoro_ghost.config import get_config_store

            cfg = get_config_store().config
            build_enabled = bool(getattr(cfg, "memory_state_links_build_enabled", True))
            target_limit = int(getattr(cfg, "memory_state_links_build_target_state_limit", 3))
            target_limit = max(0, int(target_limit))
        except Exception:  # noqa: BLE001
            build_enabled = True
            target_limit = 3

        if build_enabled and updated_state_ids_for_links:
            # --- 対象stateを絞る（直近の更新ほど優先: state_id降順） ---
            # NOTE: 1回のWritePlanで大量のstateが触られても、リンク生成は少数だけにしてコストとノイズを抑える。
            target_state_ids = sorted({int(x) for x in updated_state_ids_for_links if int(x) > 0}, reverse=True)
            if target_limit > 0:
                target_state_ids = target_state_ids[: int(target_limit)]

            if target_state_ids:
                db.add(
                    Job(
                        kind="build_state_links",
                        payload_json=common_utils.json_dumps(
                            {
                                "event_id": int(event_id),
                                "state_ids": [int(x) for x in target_state_ids],
                            }
                        ),
                        status=int(_JOB_PENDING),
                        # NOTE: 直後は embedding 更新ジョブも積まれるので、軽く遅延して混雑を避ける。
                        run_after=int(now_ts) + 3,
                        tries=0,
                        last_error=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                )

        # --- Nターンごとの整理ジョブ（chatターン回数ベース） ---
        # NOTE:
        # - 併用はしない（定期/閾値は採用しない）
        # - pending/running がある場合は二重投入しない
        if not _has_pending_or_running_job(db, kind="tidy_memory"):
            last_watermark_event_id = _last_tidy_watermark_event_id(db)
            interval_turns = (
                int(_TIDY_CHAT_TURNS_INTERVAL_FIRST)
                if int(last_watermark_event_id) <= 0
                else int(_TIDY_CHAT_TURNS_INTERVAL)
            )
            chat_turns_since = (
                db.query(func.count(Event.event_id))
                .filter(Event.source == "chat")
                .filter(Event.event_id > int(last_watermark_event_id))
                .scalar()
            )
            chat_turns_since_i = int(chat_turns_since or 0)
            if chat_turns_since_i >= int(interval_turns):
                max_chat_event_id = (
                    db.query(func.max(Event.event_id))
                    .filter(Event.source == "chat")
                    .scalar()
                )
                watermark_event_id = int(max_chat_event_id or 0)
                if watermark_event_id > 0:
                    _enqueue_tidy_memory_job(
                        db=db,
                        now_ts=int(now_ts),
                        run_after=int(now_ts) + 5,
                        reason="chat_turn_interval",
                        watermark_event_id=int(watermark_event_id),
                    )


