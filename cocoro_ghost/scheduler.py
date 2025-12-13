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
from cocoro_ghost.unit_enums import LoopStatus, Sensitivity, SummaryScopeType, UnitKind
from cocoro_ghost.unit_models import (
    Entity,
    PayloadContract,
    PayloadEpisode,
    PayloadFact,
    PayloadLoop,
    PayloadPersona,
    PayloadSummary,
    Unit,
)


def now_utc_ts() -> int:
    return int(time.time())


def utc_week_key(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _token_budget_to_char_budget(max_inject_tokens: int) -> int:
    # 日本語でも荒く扱えるよう、ここでは固定倍率で近似する（厳密さより安全側）。
    return max(2000, max_inject_tokens * 4)


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


def _format_topic_tags(topic_tags: Optional[str]) -> List[str]:
    if not topic_tags:
        return []
    try:
        loaded = json.loads(topic_tags)
        if isinstance(loaded, list):
            return [str(x) for x in loaded]
    except Exception:  # noqa: BLE001
        pass
    return [x.strip() for x in topic_tags.split(",") if x.strip()]


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


def build_memory_pack(
    *,
    db: Session,
    llm_client: LlmClient,
    user_text: str,
    image_summaries: Sequence[str] | None,
    client_context: Dict[str, Any] | None,
    now_ts: int,
    max_inject_tokens: int,
    sensitivity_max: int,
    similar_episode_k: int,
) -> str:
    max_chars = _token_budget_to_char_budget(max_inject_tokens)

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

    capsule_json = None
    # Capsuleは任意（実装が未導入でも空でよい）

    # Facts（簡易：関連entity無しでも、上位をスコアで取得）
    fact_rows: List[tuple[Unit, PayloadFact]] = (
        db.query(Unit, PayloadFact)
        .join(PayloadFact, PayloadFact.unit_id == Unit.id)
        .filter(Unit.kind == int(UnitKind.FACT), Unit.state.in_([0, 1, 2]), Unit.sensitivity <= sensitivity_max)
        .all()
    )
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
    summary_row = (
        db.query(Unit, PayloadSummary)
        .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.SUMMARY),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= sensitivity_max,
            PayloadSummary.scope_type == int(SummaryScopeType.RELATIONSHIP),
            PayloadSummary.scope_key == week_key,
        )
        .order_by(Unit.created_at.desc())
        .first()
    )
    if summary_row:
        _, ps = summary_row
        summary_texts.append(ps.summary_text.strip())

    loop_rows: List[tuple[Unit, PayloadLoop]] = (
        db.query(Unit, PayloadLoop)
        .join(PayloadLoop, PayloadLoop.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.LOOP),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= sensitivity_max,
            PayloadLoop.status == int(LoopStatus.OPEN),
        )
        .order_by(PayloadLoop.due_at.asc().nulls_last(), Unit.created_at.desc())
        .limit(8)
        .all()
    )
    loop_lines = [f"- {pl.loop_text.strip()}" for _u, pl in loop_rows if pl.loop_text.strip()]

    # Episode evidence（KNN）
    evidence_lines: List[str] = []
    intent = classify_intent_rule_based(user_text)
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

    parts: List[str] = []
    parts.append(section("PERSONA_ANCHOR", [persona_text.strip()] if persona_text else []))
    parts.append(section("RELATIONSHIP_CONTRACT", [contract_text.strip()] if contract_text else []))
    if capsule_json:
        parts.append(section("CONTEXT_CAPSULE", [capsule_json]))
    else:
        parts.append(section("CONTEXT_CAPSULE", capsule_parts))
    parts.append(section("STABLE_FACTS", fact_lines))
    parts.append(section("SHARED_NARRATIVE", summary_texts))
    parts.append(section("OPEN_LOOPS", loop_lines))
    parts.append(section("EPISODE_EVIDENCE", evidence_lines))

    pack = "".join(parts)
    if len(pack) <= max_chars:
        return pack
    return pack[:max_chars] + "\n"

