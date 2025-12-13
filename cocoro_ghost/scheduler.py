"""MemoryPack生成（取得計画器）。"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from cocoro_ghost.db import search_similar_unit_ids
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost import prompts
from cocoro_ghost.unit_enums import EntityType, LoopStatus, Sensitivity, SummaryScopeType, UnitKind
from cocoro_ghost.unit_models import (
    Entity,
    EntityAlias,
    PayloadContract,
    PayloadCapsule,
    PayloadEpisode,
    PayloadFact,
    PayloadLoop,
    PayloadPersona,
    PayloadSummary,
    Unit,
    UnitEntity,
)


def now_utc_ts() -> int:
    return int(time.time())


def utc_week_key(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _token_budget_to_char_budget(max_inject_tokens: int) -> int:
    # 日本語でも荒く扱えるよう、ここでは固定倍率で近似する（厳密さより安全側）。
    # max_inject_tokens を上限として扱う（厳密なtoken計測は行わない）。
    return max(0, max_inject_tokens * 4)


@dataclass(frozen=True)
class IntentResult:
    intent: str
    need_evidence: bool
    need_loops: bool
    suggest_summary_scope: List[str]
    sensitivity_max: int


def classify_intent_rule_based(user_text: str) -> IntentResult:
    t = (user_text or "").strip()
    recall_kw = ["覚えて", "思い出", "前に", "この前", "昔", "いつ", "どこ", "確認", "なんだっけ"]
    settings_kw = ["設定", "プロンプト", "キャラ", "口調", "変更", "変えて"]
    task_kw = ["TODO", "やること", "タスク", "締切", "予定", "リマインド"]

    intent = "smalltalk"
    if any(k in t for k in settings_kw):
        intent = "settings"
    elif any(k in t for k in task_kw):
        intent = "task"
    elif any(k in t for k in recall_kw):
        intent = "recall"

    need_evidence = intent in ("recall", "confirm")
    need_loops = True
    return IntentResult(
        intent=intent,
        need_evidence=need_evidence,
        need_loops=need_loops,
        suggest_summary_scope=["weekly", "person", "topic"],
        sensitivity_max=int(Sensitivity.PRIVATE),
    )


def _parse_intent_json(user_text: str, data: Any) -> IntentResult:
    fallback = classify_intent_rule_based(user_text)
    if not isinstance(data, dict):
        return fallback

    intent = str(data.get("intent") or "").strip()
    allowed = {"smalltalk", "counsel", "task", "settings", "recall", "confirm", "meta"}
    if intent not in allowed:
        intent = fallback.intent

    def _to_bool(v: Any, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes", "y"):
                return True
            if s in ("false", "0", "no", "n"):
                return False
        return default

    need_evidence = _to_bool(data.get("need_evidence"), default=True)
    need_loops = _to_bool(data.get("need_loops"), default=True)

    scope_raw = data.get("suggest_summary_scope")
    suggest_summary_scope: List[str] = []
    if isinstance(scope_raw, list):
        for x in scope_raw:
            s = str(x).strip()
            if s:
                suggest_summary_scope.append(s)
    if not suggest_summary_scope:
        suggest_summary_scope = fallback.suggest_summary_scope

    sens_raw = data.get("sensitivity_max")
    try:
        sens_i = int(sens_raw)
    except Exception:  # noqa: BLE001
        sens_i = int(fallback.sensitivity_max)
    sens_i = max(int(Sensitivity.NORMAL), min(int(Sensitivity.SECRET), sens_i))

    return IntentResult(
        intent=intent,
        need_evidence=need_evidence,
        need_loops=need_loops,
        suggest_summary_scope=suggest_summary_scope,
        sensitivity_max=sens_i,
    )


def classify_intent(*, llm_client: LlmClient, user_text: str) -> IntentResult:
    """軽量intent分類（JSON）。失敗時はルールベースへフォールバック。"""
    try:
        resp = llm_client.generate_json_response(
            system_prompt=prompts.get_intent_classify_prompt(),
            user_text=user_text,
            temperature=0.0,
            max_tokens=256,
        )
        data = json.loads(llm_client.response_content(resp))
        return _parse_intent_json(user_text, data)
    except Exception:  # noqa: BLE001
        return classify_intent_rule_based(user_text)


def _format_topic_tags(topic_tags: Optional[str]) -> List[str]:
    if not topic_tags:
        return []
    try:
        loaded = json.loads(topic_tags)
        if isinstance(loaded, list):
            from cocoro_ghost.topic_tags import canonicalize_topic_tags

            return canonicalize_topic_tags(loaded)
    except Exception:  # noqa: BLE001
        pass
    from cocoro_ghost.topic_tags import canonicalize_topic_tags

    return canonicalize_topic_tags([x.strip() for x in topic_tags.split(",") if x.strip()])


def _format_fact_line(
    *,
    subject: Optional[str],
    predicate: str,
    obj_text: Optional[str],
) -> str:
    s = subject or "USER"
    o = obj_text or ""
    o = o.strip()
    if o:
        return f"- {s} {predicate} {o}"
    return f"- {s} {predicate}"


def _recency_score(now: int, occurred_at: Optional[int], tau_days: float = 30.0) -> float:
    if occurred_at is None:
        return 0.0
    tau = max(1.0, tau_days * 86400.0)
    return float(math.exp(-max(0, now - occurred_at) / tau))


def _fact_score(now: int, unit: Unit) -> float:
    pin_boost = 1.0 if unit.pin else 0.0
    rec = _recency_score(now, unit.occurred_at, tau_days=45.0)
    return 0.45 * float(unit.confidence or 0.0) + 0.25 * float(unit.salience or 0.0) + 0.20 * rec + 0.10 * pin_boost


def _resolve_entity_ids_from_text(db: Session, text: str) -> set[int]:
    t = (text or "").strip()
    if not t:
        return set()

    ids: set[int] = set()
    for entity_id, alias in db.query(EntityAlias.entity_id, EntityAlias.alias).all():
        a = (alias or "").strip()
        if a and a in t:
            ids.add(int(entity_id))

    for entity_id, name in db.query(Entity.id, Entity.name).all():
        n = (name or "").strip()
        if n and n in t:
            ids.add(int(entity_id))

    return ids


def _get_user_entity_id(db: Session) -> Optional[int]:
    row = (
        db.query(Entity.id)
        .filter(Entity.etype == int(EntityType.PERSON), Entity.normalized == "user")
        .order_by(Entity.id.asc())
        .limit(1)
        .scalar()
    )
    return int(row) if row is not None else None


def build_memory_pack(
    *,
    db: Session,
    llm_client: LlmClient,
    user_text: str,
    image_summaries: Sequence[str] | None,
    client_context: Dict[str, Any] | None,
    now_ts: int,
    max_inject_tokens: int,
    similar_episode_k: int,
    intent: IntentResult,
) -> str:
    max_chars = _token_budget_to_char_budget(max_inject_tokens)
    sensitivity_max = int(intent.sensitivity_max)

    persona_text = (
        db.query(PayloadPersona.persona_text)
        .join(Unit, Unit.id == PayloadPersona.unit_id)
        .filter(PayloadPersona.is_active == 1, Unit.kind == int(UnitKind.PERSONA), Unit.sensitivity <= sensitivity_max)
        .order_by(Unit.created_at.desc())
        .limit(1)
        .scalar()
    )
    contract_text = (
        db.query(PayloadContract.contract_text)
        .join(Unit, Unit.id == PayloadContract.unit_id)
        .filter(PayloadContract.is_active == 1, Unit.kind == int(UnitKind.CONTRACT), Unit.sensitivity <= sensitivity_max)
        .order_by(Unit.created_at.desc())
        .limit(1)
        .scalar()
    )

    capsule_json: Optional[str] = None
    cap_row = (
        db.query(Unit, PayloadCapsule)
        .join(PayloadCapsule, PayloadCapsule.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.CAPSULE),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= sensitivity_max,
            (PayloadCapsule.expires_at.is_(None) | (PayloadCapsule.expires_at > now_ts)),
        )
        .order_by(Unit.created_at.desc(), Unit.id.desc())
        .first()
    )
    if cap_row:
        _cu, cap = cap_row
        capsule_json = (cap.capsule_json or "").strip() or None

    # Facts（intent→entity解決→スコアで上位）
    entity_text = "\n".join(filter(None, [user_text, *(image_summaries or [])]))
    matched_entity_ids = _resolve_entity_ids_from_text(db, entity_text)
    user_entity_id = _get_user_entity_id(db)
    fact_entity_ids = set(matched_entity_ids)
    if user_entity_id is not None:
        fact_entity_ids.add(user_entity_id)

    fact_q = (
        db.query(Unit, PayloadFact)
        .join(PayloadFact, PayloadFact.unit_id == Unit.id)
        .filter(Unit.kind == int(UnitKind.FACT), Unit.state.in_([0, 1, 2]), Unit.sensitivity <= sensitivity_max)
    )
    if fact_entity_ids:
        ids = sorted(fact_entity_ids)
        fact_q = fact_q.filter(
            (Unit.pin == 1)
            | (PayloadFact.subject_entity_id.in_(ids))
            | (PayloadFact.object_entity_id.in_(ids))
            | (PayloadFact.subject_entity_id.is_(None))
        )
        fact_rows: List[tuple[Unit, PayloadFact]] = fact_q.all()
    else:
        recent_rows: List[tuple[Unit, PayloadFact]] = fact_q.order_by(Unit.created_at.desc()).limit(200).all()
        pinned_rows: List[tuple[Unit, PayloadFact]] = fact_q.filter(Unit.pin == 1).all()
        by_id: Dict[int, tuple[Unit, PayloadFact]] = {int(u.id): (u, f) for u, f in recent_rows}
        for u, f in pinned_rows:
            by_id.setdefault(int(u.id), (u, f))
        fact_rows = list(by_id.values())

    fact_rows.sort(key=lambda r: _fact_score(now_ts, r[0]), reverse=True)
    fact_rows = fact_rows[: min(12, len(fact_rows))]

    entity_by_id: Dict[int, str] = {}
    if fact_rows:
        entity_ids: set[int] = set()
        for _u, f in fact_rows:
            if f.subject_entity_id:
                entity_ids.add(int(f.subject_entity_id))
            if f.object_entity_id:
                entity_ids.add(int(f.object_entity_id))
        if entity_ids:
            for e in db.query(Entity).filter(Entity.id.in_(sorted(entity_ids))).all():
                entity_by_id[int(e.id)] = e.name

    fact_lines: List[str] = []
    for _u, f in fact_rows:
        subject = entity_by_id.get(int(f.subject_entity_id)) if f.subject_entity_id else None
        obj = entity_by_id.get(int(f.object_entity_id)) if f.object_entity_id else f.object_text
        fact_lines.append(_format_fact_line(subject=subject, predicate=f.predicate, obj_text=obj))

    week_key = utc_week_key(now_ts)
    summary_texts: List[str] = []
    scopes = intent.suggest_summary_scope or ["weekly", "person", "topic"]

    def add_summary(scope_type: int, scope_key: Optional[str], *, fallback_latest: bool = False) -> None:
        base_q = (
            db.query(Unit, PayloadSummary)
            .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
            .filter(
                Unit.kind == int(UnitKind.SUMMARY),
                Unit.state.in_([0, 1, 2]),
                Unit.sensitivity <= sensitivity_max,
                PayloadSummary.scope_type == int(scope_type),
            )
        )

        row = None
        if scope_key is not None:
            row = (
                base_q.filter(PayloadSummary.scope_key == scope_key)
                .order_by(Unit.created_at.desc(), Unit.id.desc())
                .first()
            )
        if row is None and fallback_latest:
            row = base_q.order_by(Unit.created_at.desc(), Unit.id.desc()).first()
        if row:
            _su, ps = row
            text_ = (ps.summary_text or "").strip()
            if text_:
                summary_texts.append(text_)

    if "weekly" in scopes:
        # 現週のサマリがまだ無い場合は最新のRELATIONSHIPサマリを注入する
        add_summary(int(SummaryScopeType.RELATIONSHIP), week_key, fallback_latest=True)

    if matched_entity_ids and ("person" in scopes or "topic" in scopes):
        ents = db.query(Entity).filter(Entity.id.in_(sorted(matched_entity_ids))).all()
        if "person" in scopes:
            for e in ents:
                if int(e.etype) != int(EntityType.PERSON):
                    continue
                add_summary(int(SummaryScopeType.PERSON), f"person:{int(e.id)}")
        if "topic" in scopes:
            for e in ents:
                if int(e.etype) != int(EntityType.TOPIC):
                    continue
                key = (e.normalized or e.name or "").strip().lower()
                if key:
                    add_summary(int(SummaryScopeType.TOPIC), f"topic:{key}")

    loop_lines: List[str] = []
    if intent.need_loops:
        loop_base = (
            db.query(Unit, PayloadLoop)
            .join(PayloadLoop, PayloadLoop.unit_id == Unit.id)
            .filter(
                Unit.kind == int(UnitKind.LOOP),
                Unit.state.in_([0, 1, 2]),
                Unit.sensitivity <= sensitivity_max,
                PayloadLoop.status == int(LoopStatus.OPEN),
            )
        )
        loop_rows: List[tuple[Unit, PayloadLoop]] = []
        if matched_entity_ids:
            entity_ids = sorted(matched_entity_ids)
            cand = (
                loop_base.join(UnitEntity, UnitEntity.unit_id == Unit.id)
                .filter(UnitEntity.entity_id.in_(entity_ids))
                .order_by(PayloadLoop.due_at.asc().nulls_last(), Unit.created_at.desc(), Unit.id.desc())
                .limit(16)
                .all()
            )
            seen: set[int] = set()
            for u, pl in cand:
                if int(u.id) in seen:
                    continue
                seen.add(int(u.id))
                loop_rows.append((u, pl))
                if len(loop_rows) >= 8:
                    break
            if len(loop_rows) < 8:
                exclude_ids = [int(u.id) for u, _pl in loop_rows]
                more = (
                    loop_base.filter(~Unit.id.in_(exclude_ids) if exclude_ids else True)
                    .order_by(PayloadLoop.due_at.asc().nulls_last(), Unit.created_at.desc(), Unit.id.desc())
                    .limit(8 - len(loop_rows))
                    .all()
                )
                loop_rows.extend(more)
        else:
            loop_rows = (
                loop_base.order_by(PayloadLoop.due_at.asc().nulls_last(), Unit.created_at.desc(), Unit.id.desc())
                .limit(8)
                .all()
            )
        loop_lines = [f"- {pl.loop_text.strip()}" for _u, pl in loop_rows if pl.loop_text.strip()]

    # Episode evidence（KNN）
    evidence_lines: List[str] = []
    if intent.need_evidence and similar_episode_k > 0:
        embed_input = "\n".join(filter(None, [user_text, *(image_summaries or [])]))
        try:
            query_embedding = llm_client.generate_embedding([embed_input])[0]
            # 直近365日を対象（過去が巨大になっても検索が爆発しにくいように）
            d1 = (now_ts // 86400)
            d0 = d1 - 365
            knn_rows = search_similar_unit_ids(
                db,
                query_embedding=query_embedding,
                k=similar_episode_k,
                kind=int(UnitKind.EPISODE),
                max_sensitivity=sensitivity_max,
                occurred_day_range=(d0, d1),
            )
            if knn_rows:
                ids = [int(r.unit_id) for r in knn_rows]
                ep_rows = (
                    db.query(Unit, PayloadEpisode)
                    .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
                    .filter(Unit.id.in_(ids))
                    .all()
                )
                ep_by_id = {int(u.id): (u, pe) for u, pe in ep_rows}
                for r in knn_rows:
                    u_pe = ep_by_id.get(int(r.unit_id))
                    if not u_pe:
                        continue
                    u, pe = u_pe
                    ts = u.occurred_at or u.created_at
                    date_s = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    ut = (pe.user_text or "").strip().replace("\n", " ")
                    rt = (pe.reply_text or "").strip().replace("\n", " ")
                    ut = ut[:180]
                    rt = rt[:220]
                    evidence_lines.append(f'- {date_s} "{ut}" / "{rt}"')
        except Exception:
            # evidenceは補助なので、失敗しても空でよい
            evidence_lines = []

    # Context capsule（軽量）
    capsule_parts: List[str] = []
    if client_context:
        active_app = str(client_context.get("active_app") or "").strip()
        window_title = str(client_context.get("window_title") or "").strip()
        locale = str(client_context.get("locale") or "").strip()
        if active_app:
            capsule_parts.append(f"active_app: {active_app}")
        if window_title:
            capsule_parts.append(f"window_title: {window_title}")
        if locale:
            capsule_parts.append(f"locale: {locale}")
    if image_summaries:
        for s in image_summaries:
            s = (s or "").strip()
            if s:
                capsule_parts.append(f"image: {s}")

    def section(title: str, body_lines: Sequence[str]) -> str:
        if not body_lines:
            return f"[{title}]\n\n"
        return f"[{title}]\n" + "\n".join(body_lines) + "\n\n"

    def assemble(
        *,
        capsule_lines: Sequence[str],
        facts: Sequence[str],
        summaries: Sequence[str],
        loops: Sequence[str],
        evidence: Sequence[str],
    ) -> str:
        parts: List[str] = []
        parts.append(section("PERSONA_ANCHOR", [persona_text.strip()] if persona_text else []))
        parts.append(section("RELATIONSHIP_CONTRACT", [contract_text.strip()] if contract_text else []))
        parts.append(section("CONTEXT_CAPSULE", capsule_lines))
        parts.append(section("STABLE_FACTS", facts))
        parts.append(section("SHARED_NARRATIVE", summaries))
        parts.append(section("OPEN_LOOPS", loops))
        parts.append(section("EPISODE_EVIDENCE", evidence))
        return "".join(parts)

    capsule_lines: List[str] = []
    if capsule_json:
        capsule_lines.append(capsule_json)
    capsule_lines.extend(capsule_parts)

    facts = list(fact_lines)
    summaries = list(summary_texts)
    loops = list(loop_lines)
    evidence = list(evidence_lines)

    pack = assemble(capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence)
    if len(pack) <= max_chars:
        return pack

    # budget超過時は優先順に落とす（仕様: scheduler.md）
    evidence = []
    pack = assemble(capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence)
    if len(pack) <= max_chars:
        return pack

    while loops and len(pack) > max_chars:
        loops = loops[:-1]
        pack = assemble(capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence)
    if len(pack) <= max_chars:
        return pack

    if summaries:
        # まず数を絞る（relationshipを優先）
        summaries = summaries[:1]
        pack = assemble(capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence)
        if len(pack) > max_chars:
            # 次に本文を短縮
            s0 = summaries[0]
            budget = max(120, min(600, max_chars // 3))
            if len(s0) > budget:
                summaries = [s0[: budget].rstrip() + "…"]
                pack = assemble(
                    capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence
                )
    if len(pack) <= max_chars:
        return pack

    while facts and len(pack) > max_chars:
        facts = facts[:-1]
        pack = assemble(capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence)
    if len(pack) <= max_chars:
        return pack

    while capsule_lines and len(pack) > max_chars:
        capsule_lines = capsule_lines[:-1]
        pack = assemble(capsule_lines=capsule_lines, facts=facts, summaries=summaries, loops=loops, evidence=evidence)
    if len(pack) <= max_chars:
        return pack

    if max_chars <= 0:
        return ""
    return pack[: max(0, max_chars - 1)] + "\n"
