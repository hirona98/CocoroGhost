"""
Workerジョブハンドラ（WritePlan適用）

役割:
- apply_write_plan の実処理を担当する。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func

from cocoro_ghost import affect
from cocoro_ghost import common_utils
from cocoro_ghost import entity_utils
from cocoro_ghost.autonomy.contracts import derive_report_candidate_for_action_result
from cocoro_ghost.autonomy.runtime_blackboard import get_runtime_blackboard
from cocoro_ghost.storage.db import memory_session_scope
from cocoro_ghost.storage.memory_models import (
    AgendaThread,
    ActionDecision,
    ActionResult,
    AutonomyTrigger,
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
    WorldModelItem,
)
from cocoro_ghost.time_utils import parse_iso8601_to_utc_ts
from cocoro_ghost.jobs.constants import (
    JOB_PENDING as _JOB_PENDING,
    TIDY_CHAT_TURNS_INTERVAL as _TIDY_CHAT_TURNS_INTERVAL,
    TIDY_CHAT_TURNS_INTERVAL_FIRST as _TIDY_CHAT_TURNS_INTERVAL_FIRST,
)
from cocoro_ghost.jobs.handlers.common import (
    _affect_row_to_json,
    _enqueue_tidy_memory_job,
    _has_pending_or_running_job,
    _last_tidy_watermark_event_id,
    _link_row_to_json,
    _now_utc_ts,
    _state_row_to_json,
    _thread_row_to_json,
)


def _resolve_agenda_thread_id_from_action_result(*, db, action_result_row: ActionResult | None) -> str | None:
    """ActionResult から対象 agenda_thread_id を解決する。"""

    # --- action_result が無ければ対象 thread も無い ---
    if action_result_row is None:
        return None

    # --- decision に保存済みの agenda_thread_id を最優先で使う ---
    decision_id = str(action_result_row.decision_id or "").strip()
    if not decision_id:
        return None
    decision_row = db.query(ActionDecision).filter(ActionDecision.decision_id == str(decision_id)).one_or_none()
    if decision_row is None:
        return None
    agenda_thread_id = str(decision_row.agenda_thread_id or "").strip()
    if agenda_thread_id:
        return str(agenda_thread_id)

    # --- 旧データだけ trigger payload の agenda_thread_id を読む ---
    trigger_ref = str(decision_row.trigger_ref or "").strip()
    if not trigger_ref:
        return None
    trigger_row = db.query(AutonomyTrigger).filter(AutonomyTrigger.trigger_id == str(trigger_ref)).one_or_none()
    if trigger_row is None:
        return None
    trigger_payload_obj = common_utils.json_loads_maybe(str(trigger_row.payload_json or "{}"))
    if not isinstance(trigger_payload_obj, dict):
        raise RuntimeError("autonomy_trigger.payload_json is not an object")
    trigger_thread_id = str(trigger_payload_obj.get("agenda_thread_id") or "").strip()
    if not trigger_thread_id:
        return None
    return str(trigger_thread_id)


def _interaction_mode_to_label(value: str) -> str:
    """interaction_mode を人間向けラベルへ変換する。"""

    # --- 内部 enum をそのまま見せない ---
    labels = {
        "observe": "観察",
        "explore": "探索",
        "support": "支援",
        "wait": "待機",
        "idle": "待機",
        "tracking": "追跡",
    }
    key = str(value or "").strip()
    return str(labels.get(key, key or "不明"))


def _action_type_to_label(value: str) -> str:
    """action_type を人間向けラベルへ変換する。"""

    # --- 現在の思考本文では内部 enum を避ける ---
    labels = {
        "observe_screen": "画面観察",
        "observe_camera": "カメラ観察",
        "web_research": "Web調査",
        "schedule_action": "予定登録",
        "device_action": "デバイス操作",
        "move_to": "移動",
        "agent_delegate": "委譲",
    }
    key = str(value or "").strip()
    return str(labels.get(key, key or ""))


def _agenda_focus_to_text(*, agenda_kind: str, topic: str) -> str:
    """agenda kind/topic を人間向けの短い要約へ変換する。"""

    # --- current_thought は人間向け要約に寄せる ---
    kind_labels = {
        "research": "調べもの",
        "observation": "観察中",
        "reminder": "予定",
        "followup": "継続中",
        "support": "支援中",
    }
    topic_text = str(topic or "").strip()
    if not topic_text:
        return ""
    kind_label = str(kind_labels.get(str(agenda_kind or "").strip(), "注目中"))
    return f"{kind_label}: {topic_text}"


def _attention_target_to_text(*, target_type: str, target_value: str) -> str:
    """attention target を人間向けの短い要約へ変換する。"""

    # --- current_thought 本文では type:value の内部表記を避ける ---
    target_type_norm = str(target_type or "").strip()
    target_value_norm = str(target_value or "").strip()
    if not target_type_norm or not target_value_norm:
        return ""

    prefix_map = {
        "world_query": "調べもの",
        "world_goal": "目標",
        "world_space": "場所",
        "world_observation_class": "観察対象",
        "world_entity_kind": "対象種別",
        "action_type": "動作候補",
        "capability": "能力",
        "world_capability": "能力",
        "event_source": "イベント種別",
        "state_kind": "状態種別",
    }
    if target_type_norm.startswith("entity_"):
        return str(target_value_norm)
    prefix = str(prefix_map.get(target_type_norm, "注目"))
    return f"{prefix}: {target_value_norm}"


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
        persona_interest_pref_targets: list[dict[str, Any]] = []
        persona_interest_updated_state_kinds: set[str] = set()

        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        base_ts = int(ev.created_at)
        event_source = str(ev.source or "").strip()

        # --- source別の最終防御ポリシーを決める ---
        # NOTE:
        # - WritePlan の指示遵守だけに依存せず、適用層で更新可否を強制する。
        # - deliberation_decision は内部判断イベントのため、感情/好み更新を禁止する。
        # - action_result は構造化結果（result_status/useful_for_recall_hint）で感情更新可否を判定する。
        action_result_status = ""
        action_result_useful_for_recall = False
        action_result_row: ActionResult | None = None
        action_result_payload_obj: dict[str, Any] | None = None
        action_result_agenda_thread_id: str | None = None
        if event_source == "action_result":
            action_result_row = (
                db.query(ActionResult)
                .filter(ActionResult.event_id == int(event_id))
                .one_or_none()
            )
            if action_result_row is not None:
                action_result_status = str(action_result_row.result_status or "").strip()
                action_result_useful_for_recall = bool(int(action_result_row.useful_for_recall_hint or 0))
                payload_obj = common_utils.json_loads_maybe(str(action_result_row.result_payload_json or "{}"))
                if not isinstance(payload_obj, dict):
                    raise RuntimeError("action_result.result_payload_json is not an object")
                action_result_payload_obj = dict(payload_obj)
                action_result_agenda_thread_id = _resolve_agenda_thread_id_from_action_result(
                    db=db,
                    action_result_row=action_result_row,
                )

        allow_preference_updates = True
        allow_event_affect_update = True
        allow_long_mood_update = True
        if event_source == "deliberation_decision":
            allow_preference_updates = False
            allow_event_affect_update = False
            allow_long_mood_update = False
        elif event_source == "action_result":
            allow_preference_updates = False
            if action_result_status == "failed":
                allow_event_affect_update = True
                allow_long_mood_update = True
            elif action_result_status == "partial":
                allow_event_affect_update = True
                allow_long_mood_update = bool(action_result_useful_for_recall)
            elif action_result_status == "success":
                allow_event_affect_update = False
                allow_long_mood_update = bool(action_result_useful_for_recall)
            else:
                allow_event_affect_update = False
                allow_long_mood_update = False

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
        if not bool(allow_preference_updates):
            pref_updates = []

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

            # --- current_thought_state 用の注目ターゲット候補（構造情報のみ） ---
            pref_target_weight = 0.55
            if op == "confirm":
                pref_target_weight = 0.95
            elif op == "upsert_candidate":
                pref_target_weight = 0.65
            elif op == "revoke":
                pref_target_weight = 0.40
            persona_interest_pref_targets.append(
                {
                    "type": f"preference_{str(domain)}_{str(polarity)}",
                    "value": str(subject_raw)[:120],
                    "weight": float(pref_target_weight),
                }
            )

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
        if bool(allow_event_affect_update) and isinstance(ea, dict):
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
                        if bool(allow_long_mood_update) and body_text:
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
                        persona_interest_updated_state_kinds.add(str(kind))
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
                    persona_interest_updated_state_kinds.add(str(st.kind))
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
                    persona_interest_updated_state_kinds.add(str(st.kind))
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
        if bool(allow_long_mood_update) and (
            (moment_vad is not None and moment_conf > 0.0)
            or (baseline_text_candidate is not None and str(baseline_text_candidate).strip())
        ):
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
                persona_interest_updated_state_kinds.add("long_mood_state")
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

        # --- current_thought_state + agenda_threads を更新する（構造情報ベース） ---
        # NOTE:
        # - 文字列比較で「意味」を推定しない。
        # - event/entities/preferences/state更新/client_context の構造情報だけで現在の思考を組み立てる。
        # - current_thought_state は人間向け要約、agenda_threads は機械向けの本体として扱う。
        # - world_model_items（観測集約）も構造フィールドだけ使って反映する。
        recent_world_items = (
            db.query(WorldModelItem)
            .filter(WorldModelItem.active == 1)
            .order_by(WorldModelItem.freshness_at.desc(), WorldModelItem.updated_at.desc())
            .limit(12)
            .all()
        )
        persona_interest_targets: list[dict[str, Any]] = []

        # --- event source は常に軽く残す（直近の関心流れを見るため） ---
        persona_interest_targets.append(
            {
                "type": "event_source",
                "value": str(ev.source or ""),
                "weight": 0.40,
            }
        )

        # --- event annotations の entities を関心ターゲットへ反映 ---
        for ent in list(entities_norm or []):
            ent_type = str(ent.get("type") or "").strip()
            ent_name = str(ent.get("name") or "").strip()
            if not ent_type or not ent_name:
                continue
            ent_conf = float(ent.get("confidence") or 0.0)
            persona_interest_targets.append(
                {
                    "type": f"entity_{ent_type}",
                    "value": str(ent_name)[:120],
                    "weight": float(max(0.35, min(1.0, 0.55 + 0.35 * ent_conf))),
                }
            )

        # --- preference 更新の注目ターゲット（confirm/candidate/revoke を構造で区別） ---
        for item in list(persona_interest_pref_targets or []):
            if not isinstance(item, dict):
                continue
            persona_interest_targets.append(
                {
                    "type": str(item.get("type") or ""),
                    "value": str(item.get("value") or "")[:120],
                    "weight": float(item.get("weight") or 0.0),
                }
            )

        # --- 今回更新した state.kind を注目ターゲットへ反映 ---
        for state_kind in sorted({str(x or "").strip() for x in list(persona_interest_updated_state_kinds or set()) if str(x or "").strip()}):
            persona_interest_targets.append(
                {
                    "type": "state_kind",
                    "value": str(state_kind),
                    "weight": 0.50 if str(state_kind) != "long_mood_state" else 0.35,
                }
            )

        # --- action_result 系イベントは client_context の構造情報を追加で使う ---
        action_type_ctx = ""
        capability_ctx = ""
        result_status_ctx = ""
        client_ctx_obj = common_utils.json_loads_maybe(str(ev.client_context_json or ""))
        if isinstance(client_ctx_obj, dict):
            action_type_ctx = str(client_ctx_obj.get("action_type") or "").strip()
            capability_ctx = str(client_ctx_obj.get("capability") or "").strip()
            result_status_ctx = str(client_ctx_obj.get("result_status") or "").strip()
            if action_type_ctx:
                persona_interest_targets.append(
                    {
                        "type": "action_type",
                        "value": str(action_type_ctx),
                        "weight": 0.75 if result_status_ctx != "failed" else 0.55,
                    }
                )
            if capability_ctx:
                persona_interest_targets.append(
                    {
                        "type": "capability",
                        "value": str(capability_ctx),
                        "weight": 0.65 if result_status_ctx != "failed" else 0.45,
                    }
                )

        # --- world_model_items（観測集約）から構造ターゲットを追加 ---
        # NOTE:
        # - affordance.findings など自然言語本文の意味推定はしない。
        # - entity/relation/location の構造フィールドだけ使う。
        for w in list(recent_world_items or []):
            observation_class = str(w.observation_class or "").strip()
            if not observation_class:
                continue

            entity_obj = common_utils.json_loads_maybe(str(w.entity_json or "{}"))
            relation_obj = common_utils.json_loads_maybe(str(w.relation_json or "{}"))
            location_obj = common_utils.json_loads_maybe(str(w.location_json or "{}"))
            if not isinstance(entity_obj, dict):
                entity_obj = {}
            if not isinstance(relation_obj, dict):
                relation_obj = {}
            if not isinstance(location_obj, dict):
                location_obj = {}

            # --- 重み: confidence をベースに、今回 event に紐づく観測を少し上げる ---
            w_conf = float(max(0.0, min(1.0, float(w.confidence or 0.0))))
            base_weight = 0.35 + 0.50 * w_conf
            if int(w.source_event_id or 0) == int(event_id):
                base_weight += 0.15
            base_weight = float(max(0.0, min(1.0, base_weight)))

            persona_interest_targets.append(
                {
                    "type": "world_observation_class",
                    "value": str(observation_class),
                    "weight": float(base_weight),
                }
            )

            location_space = str(location_obj.get("space") or "").strip()
            if location_space:
                persona_interest_targets.append(
                    {
                        "type": "world_space",
                        "value": str(location_space),
                        "weight": float(max(0.2, base_weight - 0.15)),
                    }
                )

            capability_name_world = str(relation_obj.get("capability_name") or "").strip()
            if capability_name_world:
                persona_interest_targets.append(
                    {
                        "type": "world_capability",
                        "value": str(capability_name_world),
                        "weight": float(max(0.25, base_weight - 0.10)),
                    }
                )

            entity_kind = str(entity_obj.get("kind") or "").strip()
            if entity_kind:
                persona_interest_targets.append(
                    {
                        "type": "world_entity_kind",
                        "value": str(entity_kind),
                        "weight": float(max(0.25, base_weight - 0.10)),
                    }
                )

            # --- web_research_query は query/goal が構造フィールドとして存在する ---
            if entity_kind == "web_research_query":
                query_text = str(entity_obj.get("query") or "").strip()
                goal_text = str(entity_obj.get("goal") or "").strip()
                if query_text:
                    persona_interest_targets.append(
                        {
                            "type": "world_query",
                            "value": str(query_text)[:120],
                            "weight": float(base_weight),
                        }
                    )
                if goal_text:
                    persona_interest_targets.append(
                        {
                            "type": "world_goal",
                            "value": str(goal_text)[:120],
                            "weight": float(max(0.25, base_weight - 0.05)),
                        }
                    )

        # --- ターゲットを dedupe + weight 集約して上位へ圧縮 ---
        persona_interest_map: dict[tuple[str, str], dict[str, Any]] = {}
        for item in list(persona_interest_targets or []):
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("type") or "").strip()
            target_value = str(item.get("value") or "").strip()
            if not target_type or not target_value:
                continue
            weight = float(item.get("weight") or 0.0)
            key = (target_type, target_value)
            prev = persona_interest_map.get(key)
            if prev is None:
                persona_interest_map[key] = {
                    "type": str(target_type),
                    "value": str(target_value),
                    "weight": float(weight),
                }
            else:
                prev["weight"] = float(max(float(prev.get("weight") or 0.0), float(weight)))

        attention_targets_sorted = list(persona_interest_map.values())
        attention_targets_sorted.sort(
            key=lambda x: (
                -float(x.get("weight") or 0.0),
                str(x.get("type") or ""),
                str(x.get("value") or ""),
            )
        )
        attention_targets_sorted = attention_targets_sorted[:24]

        # --- agenda thread 候補へ落とす（現在の思考の実体） ---
        # NOTE:
        # - bookkeeping 用ターゲット（event_source/state_kind）は thread 化しない。
        # - 同一構造ターゲットは thread_key で一本化する。
        def _derive_agenda_kind(*, target_type: str, target_value: str) -> str | None:
            if target_type == "world_query":
                return "research"
            if target_type == "world_goal":
                return "research"
            if target_type == "action_type":
                if target_value in {"observe_screen", "observe_camera"}:
                    return "observation"
                if target_value == "web_research":
                    return "research"
                if target_value == "schedule_action":
                    return "reminder"
                return "followup"
            if target_type in {"capability", "world_capability"}:
                if target_value == "web_access":
                    return "research"
                if target_value == "vision_perception":
                    return "observation"
                if target_value == "schedule_alarm":
                    return "reminder"
                return "followup"
            if target_type.startswith("entity_"):
                return "followup"
            if target_type in {"world_observation_class", "world_entity_kind", "world_space"}:
                return "observation"
            return None

        # --- action_type はそのまま次候補になれる。world_query は web_research に変換する。 ---
        def _derive_next_action(*, target_type: str, target_value: str) -> tuple[str | None, dict[str, Any] | None]:
            if target_type == "world_query":
                return (
                    "web_research",
                    {
                        "query": str(target_value),
                        "goal": "",
                        "constraints": [],
                    },
                )
            return (None, None)

        # --- thread_id は thread_key から安定生成し、同一対象を継続扱いにする。 ---
        def _agenda_thread_id_for_key(thread_key: str) -> str:
            digest = hashlib.sha1(str(thread_key).encode("utf-8")).hexdigest()
            return f"agenda-{digest[:20]}"

        agenda_seed_rows: list[dict[str, Any]] = []
        for item in list(attention_targets_sorted or []):
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("type") or "").strip()
            target_value = str(item.get("value") or "").strip()
            if not target_type or not target_value:
                continue
            if target_type in {"event_source", "state_kind"}:
                continue

            agenda_kind = _derive_agenda_kind(
                target_type=str(target_type),
                target_value=str(target_value),
            )
            if not agenda_kind:
                continue

            next_action_type, next_action_payload = _derive_next_action(
                target_type=str(target_type),
                target_value=str(target_value),
            )
            agenda_seed_rows.append(
                {
                    "target_type": str(target_type),
                    "target_value": str(target_value),
                    "weight": float(item.get("weight") or 0.0),
                    "agenda_kind": str(agenda_kind),
                    "next_action_type": (str(next_action_type) if next_action_type else None),
                    "next_action_payload": (dict(next_action_payload) if isinstance(next_action_payload, dict) else None),
                }
            )
            if len(agenda_seed_rows) >= 8:
                break

        # --- 現在の思考は agenda の先頭候補から interaction_mode を決める。 ---
        interaction_mode = "observe"
        if agenda_seed_rows:
            top_seed = dict(agenda_seed_rows[0])
            top_kind = str(top_seed.get("agenda_kind") or "")
            top_action_type = str(top_seed.get("next_action_type") or "")
            if top_kind == "reminder":
                interaction_mode = "support"
            elif top_action_type in {"web_research", "agent_delegate"} or top_kind in {"research", "followup"}:
                interaction_mode = "explore"

        # --- action_result の共有候補は共通規則で決める（感情値は使わない） ---
        talk_candidate_level = "none"
        talk_candidate_reason = ""
        if event_source == "action_result":
            report_candidate = derive_report_candidate_for_action_result(
                action_type=str(action_type_ctx),
                result_status=str(action_result_status),
                result_payload=(dict(action_result_payload_obj) if action_result_payload_obj is not None else None),
            )
            talk_candidate_level = str(report_candidate["level"])
            talk_candidate_reason = str(report_candidate["reason"])

        # --- agenda_threads を upsert し、active thread を1本に絞る。 ---
        current_result_id = (str(action_result_row.result_id) if action_result_row is not None else None)
        result_target_thread_id = (
            str(action_result_agenda_thread_id)
            if action_result_agenda_thread_id
            else None
        )
        active_thread_id: str | None = None
        next_candidate_action: dict[str, Any] | None = None
        agenda_threads_debug_rows: list[dict[str, Any]] = []
        for idx, seed in enumerate(list(agenda_seed_rows or [])):
            thread_key = (
                f"{str(seed.get('agenda_kind') or '')}:"
                f"{str(seed.get('target_type') or '')}:"
                f"{str(seed.get('target_value') or '')}"
            )
            thread_id = _agenda_thread_id_for_key(str(thread_key))
            thread_row = (
                db.query(AgendaThread)
                .filter(AgendaThread.thread_key == str(thread_key))
                .one_or_none()
            )
            existing_report_candidate_level = (
                str(thread_row.report_candidate_level)
                if thread_row is not None and str(thread_row.report_candidate_level or "").strip()
                else "none"
            )
            existing_report_candidate_reason = (
                str(thread_row.report_candidate_reason)
                if thread_row is not None and str(thread_row.report_candidate_reason or "").strip()
                else None
            )
            is_result_target = (
                bool(result_target_thread_id)
                and str(thread_id) == str(result_target_thread_id)
            )
            resolved_thread_status = "open"
            next_action_type = str(seed.get("next_action_type") or "").strip() or None
            next_action_payload = (
                dict(seed.get("next_action_payload") or {})
                if isinstance(seed.get("next_action_payload"), dict)
                else {}
            )
            followup_due_at = int(base_ts) if next_action_type else None
            report_candidate_level = (
                str(talk_candidate_level)
                if bool(is_result_target) and str(talk_candidate_level) != "none"
                else str(existing_report_candidate_level)
            )
            report_candidate_reason = (
                str(talk_candidate_reason)
                if bool(is_result_target) and str(talk_candidate_reason)
                else existing_report_candidate_reason
            )

            # --- action_result で thread の状態を明示遷移させる ---
            # NOTE:
            # - 実際に実行した thread だけを終端/待機へ遷移させる。
            # - 先頭 seed 固定にすると、別 thread の結果で正本を壊してしまう。
            if event_source == "action_result" and bool(is_result_target):
                if action_result_status == "failed":
                    resolved_thread_status = "blocked"
                    followup_due_at = None
                elif action_result_status == "partial":
                    resolved_thread_status = "open"
                    followup_due_at = None
                elif action_result_status == "no_effect":
                    resolved_thread_status = "stale"
                    followup_due_at = None
                    next_action_type = None
                    next_action_payload = {}
                elif action_result_status == "success":
                    resolved_thread_status = "satisfied"
                    followup_due_at = None
                    next_action_type = None
                    next_action_payload = {}

            # --- 今回のフォーカス thread を1本だけ決める ---
            # NOTE:
            # - action_result では、完了した thread が終端なら次の open thread にフォーカスを移す。
            # - partial/blocked は、その thread 自体を current_thought の主対象として残す。
            if event_source != "action_result":
                if idx == 0:
                    resolved_thread_status = "active"
                    active_thread_id = str(thread_id)
            else:
                if bool(is_result_target) and str(resolved_thread_status) in {"open", "blocked"} and not active_thread_id:
                    active_thread_id = str(thread_id)
                elif (not bool(is_result_target)) and not active_thread_id and str(resolved_thread_status) in {"open", "blocked"}:
                    resolved_thread_status = "active"
                    active_thread_id = str(thread_id)

            priority = int(max(1, min(100, round(40 + float(seed.get("weight") or 0.0) * 60.0))))
            metadata_obj = {
                "source_target": {
                    "type": str(seed.get("target_type") or ""),
                    "value": str(seed.get("target_value") or ""),
                    "weight": float(seed.get("weight") or 0.0),
                },
                "updated_from": {
                    "event_id": int(event_id),
                    "event_source": str(ev.source or ""),
                },
            }
            if thread_row is None:
                thread_row = AgendaThread(
                    thread_id=str(thread_id),
                    thread_key=str(thread_key),
                    source_event_id=int(event_id),
                    source_result_id=(
                        str(current_result_id)
                        if bool(is_result_target) and current_result_id
                        else None
                    ),
                    kind=str(seed.get("agenda_kind") or ""),
                    topic=str(seed.get("target_value") or ""),
                    goal=(
                        str(seed.get("target_value") or "")
                        if str(seed.get("target_type") or "") == "world_goal"
                        else None
                    ),
                    status=str(resolved_thread_status),
                    priority=int(priority),
                    next_action_type=(str(next_action_type) if next_action_type else None),
                    next_action_payload_json=common_utils.json_dumps(next_action_payload),
                    followup_due_at=(int(followup_due_at) if followup_due_at is not None else None),
                    last_progress_at=int(now_ts),
                    last_result_status=(
                        str(action_result_status)
                        if bool(is_result_target) and action_result_status
                        else None
                    ),
                    report_candidate_level=str(report_candidate_level),
                    report_candidate_reason=(str(report_candidate_reason) if report_candidate_reason else None),
                    metadata_json=common_utils.json_dumps(metadata_obj),
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            else:
                thread_row.source_event_id = int(event_id)
                if bool(is_result_target):
                    thread_row.source_result_id = (str(current_result_id) if current_result_id else None)
                thread_row.kind = str(seed.get("agenda_kind") or "")
                thread_row.topic = str(seed.get("target_value") or "")
                thread_row.goal = (
                    str(seed.get("target_value") or "")
                    if str(seed.get("target_type") or "") == "world_goal"
                    else None
                )
                thread_row.status = str(resolved_thread_status)
                thread_row.priority = int(priority)
                thread_row.next_action_type = (str(next_action_type) if next_action_type else None)
                thread_row.next_action_payload_json = common_utils.json_dumps(next_action_payload)
                thread_row.followup_due_at = (int(followup_due_at) if followup_due_at is not None else None)
                thread_row.last_progress_at = int(now_ts)
                if bool(is_result_target):
                    thread_row.last_result_status = (str(action_result_status) if action_result_status else None)
                thread_row.report_candidate_level = str(report_candidate_level)
                thread_row.report_candidate_reason = (str(report_candidate_reason) if report_candidate_reason else None)
                thread_row.metadata_json = common_utils.json_dumps(metadata_obj)
                thread_row.updated_at = int(now_ts)
            db.add(thread_row)

            if str(active_thread_id or "") == str(thread_id) and str(resolved_thread_status) in {"active", "open", "blocked"}:
                if next_action_type and followup_due_at is not None:
                    next_candidate_action = {
                        "action_type": str(next_action_type),
                        "payload": dict(next_action_payload),
                    }

            agenda_threads_debug_rows.append(
                {
                    "thread_id": str(thread_id),
                    "status": str(resolved_thread_status),
                    "kind": str(seed.get("agenda_kind") or ""),
                    "topic": str(seed.get("target_value") or ""),
                    "priority": int(priority),
                    "next_action_type": (str(next_action_type) if next_action_type else None),
                    "followup_due_at": (int(followup_due_at) if followup_due_at is not None else None),
                    "report_candidate_level": str(report_candidate_level),
                }
            )

        # --- active thread は常に1本までに制限し、他は open に戻す。 ---
        active_threads_query = db.query(AgendaThread).filter(AgendaThread.status == "active")
        if active_thread_id:
            active_threads_query = active_threads_query.filter(AgendaThread.thread_id != str(active_thread_id))
        active_threads_query.update(
            {
                "status": "open",
                "updated_at": int(now_ts),
            },
            synchronize_session=False,
        )

        # --- 既存 current_thought_state を読む（単一前提） ---
        current_thought_state = (
            db.query(State)
            .filter(State.kind == "current_thought_state")
            .filter(State.searchable == 1)
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .first()
        )

        prev_interest_payload = (
            common_utils.json_loads_maybe(str(current_thought_state.payload_json or ""))
            if current_thought_state is not None
            else None
        )
        prev_updated_from_event_ids = []
        if isinstance(prev_interest_payload, dict) and isinstance(prev_interest_payload.get("updated_from_event_ids"), list):
            prev_updated_from_event_ids = [int(x) for x in list(prev_interest_payload.get("updated_from_event_ids") or []) if isinstance(x, (int, float)) and int(x) > 0]
        merged_updated_from_event_ids = []
        seen_event_ids: set[int] = set()
        for x in list(prev_updated_from_event_ids) + [int(event_id)]:
            i = int(x or 0)
            if i <= 0 or i in seen_event_ids:
                continue
            seen_event_ids.add(i)
            merged_updated_from_event_ids.append(i)
        merged_updated_from_event_ids = merged_updated_from_event_ids[-20:]

        focus_summary = ""
        focus_row_for_summary: dict[str, Any] | None = None
        if active_thread_id and agenda_threads_debug_rows:
            for item in list(agenda_threads_debug_rows or []):
                if str(item.get("thread_id") or "").strip() == str(active_thread_id):
                    focus_row_for_summary = dict(item)
                    break
        if focus_row_for_summary is None and agenda_threads_debug_rows:
            focus_row_for_summary = dict(agenda_threads_debug_rows[0])
        if isinstance(focus_row_for_summary, dict):
            focus_summary = _agenda_focus_to_text(
                agenda_kind=str(focus_row_for_summary.get("kind") or ""),
                topic=str(focus_row_for_summary.get("topic") or ""),
            )
        elif attention_targets_sorted:
            top_target = dict(attention_targets_sorted[0])
            focus_summary = _attention_target_to_text(
                target_type=str(top_target.get("type") or ""),
                target_value=str(top_target.get("value") or ""),
            )

        current_thought_payload = {
            "attention_targets": [
                {
                    "type": str(t.get("type") or ""),
                    "value": str(t.get("value") or ""),
                    "weight": float(t.get("weight") or 0.0),
                    "updated_at": int(now_ts),
                }
                for t in list(attention_targets_sorted)
            ],
            "interaction_mode": str(interaction_mode),
            "active_thread_id": (str(active_thread_id) if active_thread_id else None),
            "focus_summary": (str(focus_summary) if focus_summary else None),
            "next_candidate_action": (dict(next_candidate_action) if isinstance(next_candidate_action, dict) else None),
            "talk_candidate": {
                "level": str(talk_candidate_level),
                "reason": (str(talk_candidate_reason) if talk_candidate_reason else None),
            },
            "agenda_threads": list(agenda_threads_debug_rows),
            "updated_reason": "構造更新",
            "updated_from_event_ids": [int(x) for x in merged_updated_from_event_ids],
            "updated_from": {
                "event_id": int(event_id),
                "event_source": str(ev.source or ""),
                "action_type": (
                    str(client_ctx_obj.get("action_type") or "").strip()
                    if isinstance(client_ctx_obj, dict)
                    else ""
                ),
                "capability": (
                    str(client_ctx_obj.get("capability") or "").strip()
                    if isinstance(client_ctx_obj, dict)
                    else ""
                ),
                "result_status": (
                    str(client_ctx_obj.get("result_status") or "").strip()
                    if isinstance(client_ctx_obj, dict)
                    else ""
                ),
            },
        }

        # --- body_text は検索/人間可読の最低限（構造の要約のみ） ---
        top_labels = [
            _attention_target_to_text(
                target_type=str(t.get("type") or ""),
                target_value=str(t.get("value") or ""),
            )
            for t in list(attention_targets_sorted[:4])
            if str(t.get("type") or "").strip() and str(t.get("value") or "").strip()
        ]
        next_action_label = ""
        if isinstance(next_candidate_action, dict):
            next_action_label = _action_type_to_label(str(next_candidate_action.get("action_type") or "").strip())
        current_thought_body_text = f"現在の思考（{_interaction_mode_to_label(str(interaction_mode))}）"
        if focus_summary:
            current_thought_body_text += f": {str(focus_summary)}"
        elif top_labels:
            current_thought_body_text += f": {', '.join(top_labels)}"
        else:
            current_thought_body_text += ": （ターゲットなし）"
        if next_action_label:
            current_thought_body_text += f" / 次候補: {str(next_action_label)}"
        current_thought_body_text = str(current_thought_body_text)[:800]

        current_thought_payload_json = common_utils.json_dumps(current_thought_payload)
        current_thought_changed_state_id = 0
        if current_thought_state is None:
            st_current_thought = State(
                kind="current_thought_state",
                body_text=str(current_thought_body_text),
                payload_json=str(current_thought_payload_json),
                last_confirmed_at=int(base_ts),
                confidence=0.85,
                searchable=1,
                valid_from_ts=None,
                valid_to_ts=None,
                created_at=int(base_ts),
                updated_at=int(now_ts),
            )
            db.add(st_current_thought)
            db.flush()
            add_revision(
                entity_type="state",
                entity_id=int(st_current_thought.state_id),
                before=None,
                after=_state_row_to_json(st_current_thought),
                reason="current_thought_state を更新した",
                evidence_event_ids=[int(event_id)],
            )
            current_thought_changed_state_id = int(st_current_thought.state_id)
        else:
            before = _state_row_to_json(current_thought_state)
            same_payload = str(current_thought_state.payload_json or "") == str(current_thought_payload_json)
            same_body = str(current_thought_state.body_text or "") == str(current_thought_body_text)
            same_lca = int(current_thought_state.last_confirmed_at or 0) == int(base_ts)
            if not (same_payload and same_body and same_lca):
                current_thought_state.kind = "current_thought_state"
                current_thought_state.body_text = str(current_thought_body_text)
                current_thought_state.payload_json = str(current_thought_payload_json)
                current_thought_state.last_confirmed_at = int(base_ts)
                current_thought_state.confidence = 0.85
                current_thought_state.valid_from_ts = None
                current_thought_state.valid_to_ts = None
                current_thought_state.searchable = 1
                current_thought_state.updated_at = int(now_ts)
                db.add(current_thought_state)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(current_thought_state.state_id),
                    before=before,
                    after=_state_row_to_json(current_thought_state),
                    reason="current_thought_state を更新した",
                    evidence_event_ids=[int(event_id)],
                )
                current_thought_changed_state_id = int(current_thought_state.state_id)

        # --- entity 索引は今回 event の entities を再利用（現在の思考の検索用） ---
        if int(current_thought_changed_state_id) > 0:
            replace_state_entities(
                state_id_in=int(current_thought_changed_state_id),
                entities_norm_in=list(entities_norm),
            )
            db.add(
                Job(
                    kind="upsert_state_embedding",
                    payload_json=common_utils.json_dumps({"state_id": int(current_thought_changed_state_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

        # --- RAM blackboard の短期注目状態へも反映（次回 Deliberation の材料選別へ即時反映） ---
        get_runtime_blackboard().merge_attention_targets(
            items=[
                {
                    "type": str(t.get("type") or ""),
                    "value": str(t.get("value") or ""),
                    "weight": float(t.get("weight") or 0.0),
                }
                for t in list(attention_targets_sorted[:16])
            ],
            now_system_ts=int(now_ts),
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
