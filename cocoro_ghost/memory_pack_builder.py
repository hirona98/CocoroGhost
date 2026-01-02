"""
MemoryPack生成（取得計画器）

会話に注入する「内部コンテキスト（MemoryPack）」を組み立てる。
Facts、Summary、Loops、Episode証拠、Capsule、partner_mood_state等を
トークン予算内で優先順位に従って構築する。
"""

from __future__ import annotations

import json
import logging
import math
import time
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING

from sqlalchemy.orm import Session

from cocoro_ghost.partner_mood import compute_partner_mood_state_from_episodes
from cocoro_ghost.unit_enums import Sensitivity, UnitKind
from cocoro_ghost.unit_models import (
    Entity,
    EntityAlias,
    PayloadCapsule,
    PayloadEpisode,
    PayloadFact,
    PayloadLoop,
    PayloadSummary,
    Unit,
    UnitEntity,
)
from cocoro_ghost.partner_mood_runtime import apply_partner_mood_state_override, set_last_used
from cocoro_ghost.fact_policy import (
    canonicalize_fact_predicate,
    effective_fact_ts,
    fact_identity_key,
    is_event_predicate,
    is_exclusive_predicate,
)

if TYPE_CHECKING:
    from cocoro_ghost.llm_client import LlmClient
    from cocoro_ghost.retriever import RankedEpisode


# ロガー（MemoryPackの組み立て品質劣化など、アプリ側の警告を出す）
logger = logging.getLogger(__name__)

# MemoryPackの見出しは日常会話で衝突しにくい形式に統一する。
MEMORY_PACK_SECTION_PREFIX = "<<<COCORO_GHOST_SECTION:"
MEMORY_PACK_SECTION_SUFFIX = ">>>"
_TIME_KEYS_FOR_LLM = {"generated_at", "occurred_at", "created_at", "expires_at", "now_ts"}


def format_memory_pack_section(title: str, body_lines: Sequence[str]) -> str:
    """
    MemoryPackのセクションを整形する。

    ユーザー本文と混ざらない見出し形式で返す。
    """
    header = f"{MEMORY_PACK_SECTION_PREFIX}{title}{MEMORY_PACK_SECTION_SUFFIX}"
    if not body_lines:
        return f"{header}\n\n"
    return f"{header}\n" + "\n".join(body_lines) + "\n\n"


def _to_local_iso(ts: int | float | None) -> str | None:
    """
    UNIX秒をローカル時刻のISO文字列に変換する。
    """
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except Exception:  # noqa: BLE001
        return None


def _convert_time_fields_for_llm(obj: Any) -> Any:
    """
    LLMに渡す前提で、既知の時刻キーだけローカル時刻へ変換する。
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _TIME_KEYS_FOR_LLM and isinstance(v, (int, float)):
                out[k] = _to_local_iso(v) or v
                continue
            out[k] = _convert_time_fields_for_llm(v)
        return out
    if isinstance(obj, list):
        return [_convert_time_fields_for_llm(v) for v in obj]
    return obj


def _convert_capsule_json_for_llm(capsule_json: str | None) -> str | None:
    """
    capsule_json内の時刻をローカル時刻に変換してから返す。
    """
    if not capsule_json:
        return None
    try:
        obj = json.loads(capsule_json)
    except Exception:  # noqa: BLE001
        return capsule_json
    converted = _convert_time_fields_for_llm(obj)
    try:
        return json.dumps(converted, ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return capsule_json


def now_utc_ts() -> int:
    """
    現在時刻をUNIX秒で返す。

    UTCベースのタイムスタンプを取得する。
    """
    return int(time.time())


def _token_budget_to_char_budget(max_inject_tokens: int) -> int:
    # 日本語でも荒く扱えるよう、ここでは固定倍率で近似する（厳密さより安全側）。
    # max_inject_tokens を上限として扱う（厳密なtoken計測は行わない）。
    return max(0, max_inject_tokens * 4)


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


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
    """
    FactをMemoryPack注入用の1行に整形する。

    NOTE:
    - predicate は制御語彙（正規形）を想定する。
    - obj_text は表示のための補助であり、同一性判定には別ロジックを使う。
    """
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


def _is_fact_currently_valid(*, now_ts: int, valid_from: Optional[int], valid_to: Optional[int]) -> bool:
    """
    Factが「いま有効」かを判定する。

    - valid_from が未来なら未発効扱い
    - valid_to が過去なら期限切れ扱い
    - それ以外は有効
    """
    if isinstance(valid_from, int) and valid_from > int(now_ts):
        return False
    if isinstance(valid_to, int) and valid_to < int(now_ts):
        return False
    return True


def _pick_stable_facts_for_injection(
    *,
    now_ts: int,
    fact_rows: list[tuple[Unit, PayloadFact]],
    max_items: int,
) -> list[tuple[Unit, PayloadFact, str]]:
    """
    STABLE_FACTSへ注入するFactを選別する。

    方針:
    - predicate を制御語彙へ正規化し、未知predicateは注入しない
    - valid_from/valid_to により「現在有効」を優先（pinは例外として残す）
    - 同一三つ組は重複排除
    - exclusive は subject+predicate ごとに最新1件のみ
    - event は subject+predicate ごとに最初（最古）を優先
    - set は predicateごとに上限Kで偏りを防ぐ
    """
    # --- パラメータ（ここは“人間模倣・ドメイン非依存”のため小さく固定） ---
    per_predicate_cap = 3

    # --- 事前: スコア順（高いほど優先） ---
    fact_rows_sorted = sorted(fact_rows, key=lambda r: _fact_score(now_ts, r[0]), reverse=True)

    # --- 収集用 ---
    chosen: list[tuple[Unit, PayloadFact, str]] = []
    seen_triples: set[tuple[Optional[int], str, Optional[int], Optional[str]]] = set()
    exclusive_seen: dict[tuple[Optional[int], str], int] = {}
    event_best: dict[tuple[Optional[int], str], tuple[int, Unit, PayloadFact, str]] = {}
    per_predicate_counts: dict[str, int] = {}

    # --- 1) pin（最優先） ---
    for u, f in fact_rows_sorted:
        if not u.pin:
            continue
        canon = canonicalize_fact_predicate(f.predicate)
        if canon is None:
            continue
        # pinは選別の優先度を上げるが、期限切れ/未発効は注入しない（矛盾・誤誘導を避ける）。
        if not _is_fact_currently_valid(now_ts=now_ts, valid_from=f.valid_from, valid_to=f.valid_to):
            continue
        triple = fact_identity_key(
            subject_entity_id=int(f.subject_entity_id) if f.subject_entity_id is not None else None,
            predicate=canon,
            object_entity_id=int(f.object_entity_id) if f.object_entity_id is not None else None,
            object_text=f.object_text,
        )
        if triple in seen_triples:
            continue
        seen_triples.add(triple)
        chosen.append((u, f, canon))
        if len(chosen) >= max_items:
            return chosen[:max_items]

    # --- 2) event（履歴: 最初を優先して後で差し込む） ---
    for u, f in fact_rows_sorted:
        canon = canonicalize_fact_predicate(f.predicate)
        if canon is None or not is_event_predicate(canon):
            continue
        if not _is_fact_currently_valid(now_ts=now_ts, valid_from=f.valid_from, valid_to=f.valid_to):
            # eventの期限切れは基本落とす（pinがあれば上で残る）
            continue
        key = (int(f.subject_entity_id) if f.subject_entity_id is not None else None, canon)
        ts = effective_fact_ts(occurred_at=u.occurred_at, created_at=u.created_at, valid_from=f.valid_from)
        if key in event_best and ts >= event_best[key][0]:
            continue
        event_best[key] = (ts, u, f, canon)

    # --- 3) 通常（exclusive/set） ---
    for u, f in fact_rows_sorted:
        if len(chosen) >= max_items:
            break
        canon = canonicalize_fact_predicate(f.predicate)
        if canon is None:
            continue
        if u.pin:
            # pinは上で処理済み
            continue
        if is_event_predicate(canon):
            # eventは後で差し込む
            continue
        if not _is_fact_currently_valid(now_ts=now_ts, valid_from=f.valid_from, valid_to=f.valid_to):
            continue

        # setの偏り抑制
        per_predicate_counts.setdefault(canon, 0)
        if not is_exclusive_predicate(canon) and per_predicate_counts[canon] >= per_predicate_cap:
            continue

        subj_id = int(f.subject_entity_id) if f.subject_entity_id is not None else None
        obj_eid = int(f.object_entity_id) if f.object_entity_id is not None else None
        triple = fact_identity_key(
            subject_entity_id=subj_id,
            predicate=canon,
            object_entity_id=obj_eid,
            object_text=f.object_text,
        )
        if triple in seen_triples:
            continue

        # exclusive は subject+predicate で最新のみ
        if is_exclusive_predicate(canon):
            sp_key = (subj_id, canon)
            ts = effective_fact_ts(occurred_at=u.occurred_at, created_at=u.created_at, valid_from=f.valid_from)
            prev_ts = exclusive_seen.get(sp_key)
            if prev_ts is not None and ts <= prev_ts:
                continue
            exclusive_seen[sp_key] = ts

            # すでに同じ sp_key を入れていたら差し替える（より新しいものに更新）
            replaced = False
            for i, (_u0, f0, c0) in enumerate(list(chosen)):
                # pinはユーザー意思として最優先のため、exclusiveでも差し替えない。
                if getattr(_u0, "pin", 0):
                    continue
                if c0 != canon:
                    continue
                subj0 = int(f0.subject_entity_id) if f0.subject_entity_id is not None else None
                if subj0 != subj_id:
                    continue
                chosen[i] = (u, f, canon)
                replaced = True
                break
            if replaced:
                # triple重複抑制のため、古いtripleはseenに残る（問題ない）
                seen_triples.add(triple)
                continue

        seen_triples.add(triple)
        chosen.append((u, f, canon))
        per_predicate_counts[canon] += 1

    # --- 4) event を最後に差し込む（枠があれば） ---
    if len(chosen) < max_items and event_best:
        # eventは“最初”が価値なので、時刻の古い順で安定的に並べる。
        for _ts, u, f, canon in sorted(event_best.values(), key=lambda x: x[0]):
            if len(chosen) >= max_items:
                break
            triple = fact_identity_key(
                subject_entity_id=int(f.subject_entity_id) if f.subject_entity_id is not None else None,
                predicate=canon,
                object_entity_id=int(f.object_entity_id) if f.object_entity_id is not None else None,
                object_text=f.object_text,
            )
            if triple in seen_triples:
                continue
            seen_triples.add(triple)
            chosen.append((u, f, canon))

    return chosen[:max_items]


def extract_entity_names_with_llm(llm_client: "LlmClient", text: str) -> list[str]:
    """
    LLMでentity名だけを抽出する（names only）。

    失敗時は空配列を返す。
    """
    from cocoro_ghost import prompts
    from cocoro_ghost.llm_client import LlmRequestPurpose

    # LLM抽出はベストエフォート。失敗してもスケジューリングは継続する。
    try:
        # ここは「names only」専用の軽量プロンプトを使う（roles/relationsの推測を避ける）。
        resp = llm_client.generate_json_response(
            system_prompt=prompts.get_entity_names_only_prompt(),
            user_text=text,
            purpose=LlmRequestPurpose.ENTITY_NAME_EXTRACT,
        )
        raw = llm_client.response_content(resp)
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return []

    names_raw = data.get("names") or []
    names: list[str] = []
    if isinstance(names_raw, list):
        for n in names_raw:
            s = str(n or "").strip()
            if s:
                names.append(s)
    return names


def collect_entity_alias_rows(db: Session) -> list[tuple[int, str]]:
    """
    Entityのalias/nameを正規化して収集する。
    """
    rows: list[tuple[int, str]] = []
    # aliasの正規化一覧を先に作る。
    for entity_id, alias in db.query(EntityAlias.entity_id, EntityAlias.alias).all():
        a = _normalize_text(alias)
        if not a:
            continue
        rows.append((int(entity_id), a))
    # nameの正規化一覧も混ぜる。
    for entity_id, name in db.query(Entity.id, Entity.name).all():
        n = _normalize_text(name)
        if not n:
            continue
        rows.append((int(entity_id), n))
    return rows


def match_entity_ids(candidate_names: Sequence[str], alias_rows: Sequence[tuple[int, str]]) -> set[int]:
    """
    LLM抽出名とalias/nameを突合し、entity_idを解決する。
    """
    if not candidate_names or not alias_rows:
        return set()
    ids: set[int] = set()
    for name in candidate_names:
        nn = _normalize_text(str(name))
        if not nn:
            continue
        for entity_id, alias in alias_rows:
            if nn in alias or alias in nn:
                ids.add(int(entity_id))
    return ids


def _get_user_entity_id(db: Session) -> Optional[int]:
    row = (
        db.query(Entity.id)
        .filter(Entity.normalized == "user")
        .order_by(Entity.id.asc())
        .limit(1)
        .scalar()
    )
    return int(row) if row is not None else None


def _entity_roles(entity: Entity) -> set[str]:
    try:
        raw = json.loads(entity.roles_json or "[]")
        if isinstance(raw, list):
            return {str(x).strip().lower() for x in raw if str(x).strip()}
    except Exception:  # noqa: BLE001
        return set()
    return set()


def should_inject_episodes(relevant_episodes: Sequence["RankedEpisode"]) -> bool:
    """
    エピソード検索結果を注入すべきか判定する。

    high関連度が1件以上、またはmedium関連度が2件以上なら注入する。
    """
    if not relevant_episodes:
        return False

    high_count = sum(1 for e in relevant_episodes if e.relevance == "high")
    if high_count >= 1:
        return True

    medium_count = sum(1 for e in relevant_episodes if e.relevance == "medium")
    if medium_count >= 2:
        return True

    return False


def build_memory_pack(
    *,
    db: Session,
    user_text: str,
    image_summaries: Sequence[str] | None,
    client_context: Dict[str, Any] | None,
    now_ts: int,
    max_inject_tokens: int,
    relevant_episodes: Sequence["RankedEpisode"],
    matched_entity_ids: Sequence[int],
    injection_strategy: str | None = None,
) -> str:
    """
    MemoryPackを組み立てる。

    Facts、Summary、Loops、Episode証拠、Capsule等を
    トークン予算内で優先順位に従って組み立て、文字列として返す。
    予算超過時はEpisode証拠→Loops→Summary→Factsの順に削減する。
    """
    max_chars = _token_budget_to_char_budget(max_inject_tokens)
    # 一旦「注入（引き出し）」を無制限にする（SECRETまで許可）。
    sensitivity_max = int(Sensitivity.SECRET)

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

    def _partner_mood_guidance_from_state(state: dict[str, Any]) -> str | None:
        """partner_mood_state を本文の口調へ落とし込むための短い指示（内部向け）。

        - LLMは数値の解釈がブレることがあるため、言語化したガイダンスを併記する。
        - ユーザーには見せない前提（MemoryPack内）。
        """
        try:
            label = str(state.get("label") or "").strip()
            intensity = float(state.get("intensity") or 0.0)
            response_policy = state.get("response_policy") if isinstance(state.get("response_policy"), dict) else {}
            refusal_allowed = bool(response_policy.get("refusal_allowed"))
        except Exception:  # noqa: BLE001
            return None

        if label == "anger":
            return "機嫌: 怒り（強）。口調は不機嫌/苛立ちを明確に。愛情表現は控える。"
        if label == "sadness":
            return "機嫌: 悲しみ。口調は落ち込み気味で、優しい言葉を増やす。"
        if label == "fear":
            return "機嫌: 不安/恐れ。慎重で確認が多め。安心材料を求める。"
        if label == "joy":
            return "機嫌: 喜び。明るく、親しみやすく。"
        if label == "neutral":
            return "機嫌: 落ち着き。淡々と、安定した口調。"

        return None

    # Facts（intent→entity解決→スコアで上位）
    matched_entity_ids = {int(eid) for eid in matched_entity_ids if eid is not None}
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
    # 注入は「重複排除」「期限」「競合解決（exclusive/event）」を反映してから上限を適用する。
    picked = _pick_stable_facts_for_injection(now_ts=now_ts, fact_rows=fact_rows, max_items=12)

    entity_by_id: Dict[int, str] = {}
    if picked:
        entity_ids: set[int] = set()
        for _u, f, _canon in picked:
            if f.subject_entity_id:
                entity_ids.add(int(f.subject_entity_id))
            if f.object_entity_id:
                entity_ids.add(int(f.object_entity_id))
        if entity_ids:
            for e in db.query(Entity).filter(Entity.id.in_(sorted(entity_ids))).all():
                entity_by_id[int(e.id)] = e.name

    fact_lines: List[str] = []
    for _u, f, canon in picked:
        subject = entity_by_id.get(int(f.subject_entity_id)) if f.subject_entity_id else None
        obj = entity_by_id.get(int(f.object_entity_id)) if f.object_entity_id else f.object_text
        fact_lines.append(_format_fact_line(subject=subject, predicate=canon, obj_text=obj))

    rolling_scope_key = "rolling:7d"
    summary_texts: List[str] = []
    relationship_lines: List[str] = []
    scopes = ["bond", "person", "topic"]

    def add_summary(scope_label: str, scope_key: Optional[str], *, fallback_latest: bool = False) -> None:
        """指定スコープのサマリを1つ取り出してsummary_textsへ追加する（無ければ何もしない）。"""
        base_q = (
            db.query(Unit, PayloadSummary)
            .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
            .filter(
                Unit.kind == int(UnitKind.SUMMARY),
                Unit.state.in_([0, 1, 2]),
                Unit.sensitivity <= sensitivity_max,
                PayloadSummary.scope_label == str(scope_label),
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

    if "bond" in scopes:
        # rolling（直近7日）のbondサマリが無い場合は最新のbondサマリを注入する
        add_summary("bond", rolling_scope_key, fallback_latest=True)

    if matched_entity_ids and ("person" in scopes or "topic" in scopes):
        # LLMで抽出されたentity名に一致するものだけを扱う（余計な人物は入れない）。
        ents = db.query(Entity).filter(Entity.id.in_(sorted(matched_entity_ids))).all()
        person_ids: list[int] = []
        entity_name_by_id: dict[int, str] = {}
        for e in ents:
            entity_name_by_id[int(e.id)] = e.name
            roles = _entity_roles(e)
            # 会話で触れている人物だけ person summary を注入する。
            if "person" in scopes and "person" in roles:
                person_ids.append(int(e.id))
                add_summary("person", f"person:{int(e.id)}")
            # topic summary も同様に会話に出た話題のみ注入する。
            if "topic" in scopes and "topic" in roles:
                key = (e.normalized or e.name or "").strip().lower()
                if key:
                    add_summary("topic", f"topic:{key}")

        # 関係性は person summary JSON の favorability_score をそのまま注入する。
        if person_ids:
            rows = (
                db.query(Unit, PayloadSummary)
                .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
                .filter(
                    Unit.kind == int(UnitKind.SUMMARY),
                    Unit.state.in_([0, 1, 2]),
                    Unit.sensitivity <= sensitivity_max,
                    PayloadSummary.scope_label == "person",
                    PayloadSummary.scope_key.in_([f"person:{pid}" for pid in person_ids]),
                )
                .order_by(Unit.created_at.desc(), Unit.id.desc())
                .all()
            )
            latest_by_person: dict[int, PayloadSummary] = {}
            for _u, ps in rows:
                scope_key = str(ps.scope_key or "")
                if not scope_key.startswith("person:"):
                    continue
                try:
                    pid = int(scope_key.split("person:", 1)[1])
                except Exception:  # noqa: BLE001
                    continue
                if pid in latest_by_person:
                    continue
                latest_by_person[pid] = ps
            # 直近の更新順で最大5件まで注入する。
            for pid, ps in list(latest_by_person.items())[:5]:
                try:
                    summary_obj = json.loads(ps.summary_json or "{}")
                except Exception:  # noqa: BLE001
                    continue
                score_raw = summary_obj.get("favorability_score")
                try:
                    score = float(score_raw)
                except Exception:  # noqa: BLE001
                    continue
                name = entity_name_by_id.get(pid, f"person:{pid}")
                relationship_lines.append(f"- person_id={pid} name={name} favorability_score={score:.2f}")

    loop_lines: List[str] = []
    loop_base = (
        db.query(Unit, PayloadLoop)
        .join(PayloadLoop, PayloadLoop.unit_id == Unit.id)
        .filter(
            Unit.kind == int(UnitKind.LOOP),
            Unit.state.in_([0, 1, 2]),
            Unit.sensitivity <= sensitivity_max,
            PayloadLoop.expires_at > now_ts,
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

    # Episode evidence（Retriever）
    evidence_lines: List[str] = []
    if should_inject_episodes(relevant_episodes):
        strategy = (injection_strategy or "quote_key_parts").strip() or "quote_key_parts"
        if strategy not in {"quote_key_parts", "summarize", "full"}:
            strategy = "quote_key_parts"

        def _norm(text: str) -> str:
            return " ".join((text or "").replace("\r", "").replace("\n", " ").split())

        def _truncate(text: str, limit: int) -> str:
            t = _norm(text)
            if limit <= 0 or len(t) <= limit:
                return t
            return t[:limit].rstrip() + "…"

        if strategy == "full":
            user_limit, reply_limit = 420, 520
        elif strategy == "summarize":
            user_limit, reply_limit = 180, 220
        else:
            user_limit, reply_limit = 120, 160

        evidence_lines.append("以下は現在の会話に関連する過去のやりとりです。")
        evidence_lines.append("")
        for e in relevant_episodes:
            ts = int(e.occurred_at)
            date_s = _to_local_iso(ts) or ""
            if date_s:
                evidence_lines.append(f"[{date_s}]")

            ut = _truncate(e.user_text, user_limit)
            rt = _truncate(e.reply_text, reply_limit)
            reason = _truncate(e.reason, 180)

            if strategy == "summarize":
                combined = _truncate(" / ".join([x for x in [ut, rt] if x]), 380)
                evidence_lines.append(f"要点: {combined}")
            else:
                if ut:
                    evidence_lines.append(f'User: 「{ut}」')
                if rt:
                    evidence_lines.append(f'Partner: 「{rt}」')

            if reason:
                evidence_lines.append(f"→ 関連: {reason}")
            evidence_lines.append("")

    # Context capsule（軽量）
    capsule_parts: List[str] = []
    if capsule_json:
        # LLM向けに時刻だけローカルへ変換する。
        capsule_local = _convert_capsule_json_for_llm(capsule_json)
        capsule_parts.append(f"capsule_json: {capsule_local}")
    now_local = datetime.fromtimestamp(now_ts).astimezone().isoformat()
    capsule_parts.append(f"now_local: {now_local}")
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
        for idx, s in enumerate(image_summaries, start=1):
            s = (s or "").strip()
            if not s:
                continue
            capsule_parts.append(f"[画像 #{idx}]")
            capsule_parts.append("---IMAGE_SUMMARY_START---")
            capsule_parts.extend(s.splitlines())
            capsule_parts.append("---IMAGE_SUMMARY_END---")
            capsule_parts.append("")

    # AI人格の感情（重要度×時間減衰）を同期計算して注入する。
    #
    # 目的:
    # - /api/chat は「返信生成の前」に MemoryPack を組むため、capsule_refresh（Worker）がまだ走っていないと
    #   "partner_mood_state" が注入されず、感情の反映が1ターン遅れやすい。
    # - ここで同期計算して `CONTEXT_CAPSULE` に入れることで、「直前の出来事」まで含めた機嫌を次ターンから使える。
    #
    # 計算式（partner_mood の積分ロジック）:
    #   impact = partner_affect_intensity × salience × confidence × exp(-Δt/τ(salience))
    # - salience が高いほど τ が長い → 大事件が残る
    # - salience が低いほど τ が短い → 雑談はすぐ消える
    try:
        partner_mood_units = (
            db.query(Unit, PayloadEpisode)
            .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
            .filter(
                Unit.kind == int(UnitKind.EPISODE),
                Unit.state.in_([0, 1, 2]),
                Unit.sensitivity <= sensitivity_max,
                Unit.partner_affect_label.isnot(None),
            )
            .order_by(Unit.created_at.desc(), Unit.id.desc())
            .limit(500)
            .all()
        )
        partner_mood_episodes = []
        # partner_response_policy は直近だけ見れば十分なので、JSON parse は上位N件に限定する。
        partner_response_policy_parse_limit = 60
        for idx, (u, pe) in enumerate(partner_mood_units):
            partner_response_policy = None
            if idx < partner_response_policy_parse_limit and (pe.reflection_json or "").strip():
                try:
                    obj = json.loads(pe.reflection_json)
                    pp = obj.get("partner_response_policy") if isinstance(obj, dict) else None
                    partner_response_policy = pp if isinstance(pp, dict) else None
                except Exception:  # noqa: BLE001
                    partner_response_policy = None
            partner_mood_episodes.append(
                {
                    "occurred_at": int(u.occurred_at) if u.occurred_at is not None else None,
                    "created_at": int(u.created_at),
                    "partner_affect_label": u.partner_affect_label,
                    "partner_affect_intensity": u.partner_affect_intensity,
                    "salience": u.salience,
                    "confidence": u.confidence,
                    # /api/chat の内部JSONで出た「方針ノブ」を次ターン以降にも効かせる。
                    "partner_response_policy": partner_response_policy,
                }
            )
        partner_mood_state = compute_partner_mood_state_from_episodes(partner_mood_episodes, now_ts=now_ts)
        # デバッグ用: UI/API から in-memory ランタイム状態を適用する
        partner_mood_state = apply_partner_mood_state_override(partner_mood_state, now_ts=now_ts)
        compact = {
            "label": partner_mood_state.get("label"),
            "intensity": partner_mood_state.get("intensity"),
            "components": partner_mood_state.get("components"),
            "response_policy": partner_mood_state.get("response_policy"),
            "now_local": _to_local_iso(now_ts),
        }
        # UI向け: 前回チャットで使った値（注入した値）を保存する。
        # ここでの値が、次のチャットの直前状態として扱われる。
        try:
            set_last_used(now_ts=now_ts, state=compact)
        except Exception:  # noqa: BLE001
            pass
        capsule_parts.append(
            f"partner_mood_state: {json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"
        )

        # partner_mood_state を口調へ確実に反映させるため、短い言語ガイドを併記する。
        # ※ capsule_json（過去の状態や直近のJoy発話など）に強く引っ張られないよう、こちらを優先材料にする。
        guidance = _partner_mood_guidance_from_state(
            partner_mood_state if isinstance(partner_mood_state, dict) else {}
        )
        if guidance:
            capsule_parts.append(f"partner_mood_guidance: {guidance}")
    except Exception:  # noqa: BLE001
        pass

    def assemble(
        *,
        capsule_lines: Sequence[str],
        facts: Sequence[str],
        summaries: Sequence[str],
        relationship: Sequence[str],
        loops: Sequence[str],
        evidence: Sequence[str],
    ) -> str:
        """各セクションを結合してMemoryPack全体を生成する。"""
        parts: List[str] = []
        parts.append(format_memory_pack_section("CONTEXT_CAPSULE", capsule_lines))
        parts.append(format_memory_pack_section("STABLE_FACTS", facts))
        parts.append(format_memory_pack_section("SHARED_NARRATIVE", summaries))
        parts.append(format_memory_pack_section("RELATIONSHIP_STATE", relationship))
        parts.append(format_memory_pack_section("OPEN_LOOPS", loops))
        parts.append(format_memory_pack_section("EPISODE_EVIDENCE", evidence))
        return "".join(parts)

    # NOTE:
    # - capsule_json と同期計算した partner_mood_state の両方を注入する。
    capsule_lines: List[str] = []
    capsule_lines.extend(capsule_parts)

    facts = list(fact_lines)
    summaries = list(summary_texts)
    relationship = list(relationship_lines)

    # 怒りが強いときは「関係性サマリ（大好き等）」が口調を上書きしやすい。
    # ここでは短期状態（partner_mood_state）を優先し、SharedNarrativeを注入しない。
    try:
        if isinstance(partner_mood_state, dict):
            if str(partner_mood_state.get("label") or "") == "anger" and float(partner_mood_state.get("intensity") or 0.0) >= 0.6:
                summaries = []
                relationship = []
    except Exception:  # noqa: BLE001
        pass
    loops = list(loop_lines)
    evidence = list(evidence_lines)

    # 予算超過時の警告は「削減が発生した場合のみ」1回だけ出す。
    # NOTE: ここでは token 計測ではなく文字数（max_inject_tokens*4近似）を使う。
    trim_steps: list[str] = []
    original_pack_chars: int | None = None

    def _return_with_budget_warning(final_pack: str) -> str:
        """
        MemoryPackの注入予算超過により削減した場合のWARNINGを出して返す。

        - 1回だけログを出す前提（呼び出し側が超過時にのみ呼ぶ）。
        - 末尾にユーザー指定の文言を必ず付与する。
        """
        try:
            logger.warning(
                "MemoryPackがトークン予算を超過したため内容を削減しました: max_inject_tokens=%s max_chars=%s original_chars=%s final_chars=%s steps=%s。最大トークンを増やすか会話履歴数を減らしてください",
                int(max_inject_tokens),
                int(max_chars),
                int(original_pack_chars or 0),
                int(len(final_pack or "")),
                ",".join(trim_steps) if trim_steps else "(no_steps)",
            )
        except Exception:  # noqa: BLE001
            pass
        return final_pack

    pack = assemble(
        capsule_lines=capsule_lines,
        facts=facts,
        summaries=summaries,
        relationship=relationship,
        loops=loops,
        evidence=evidence,
    )
    original_pack_chars = len(pack)
    if len(pack) <= max_chars:
        return pack

    # budget超過時は優先順に落とす（仕様: docs/memory_pack_builder.md）
    evidence = []
    trim_steps.append("EPISODE_EVIDENCE削除")
    pack = assemble(
        capsule_lines=capsule_lines,
        facts=facts,
        summaries=summaries,
        relationship=relationship,
        loops=loops,
        evidence=evidence,
    )
    if len(pack) <= max_chars:
        return _return_with_budget_warning(pack)

    while loops and len(pack) > max_chars:
        loops = loops[:-1]
        if not trim_steps or not trim_steps[-1].startswith("OPEN_LOOPS削減"):
            trim_steps.append("OPEN_LOOPS削減")
        pack = assemble(
            capsule_lines=capsule_lines,
            facts=facts,
            summaries=summaries,
            relationship=relationship,
            loops=loops,
            evidence=evidence,
        )
    if len(pack) <= max_chars:
        return _return_with_budget_warning(pack)

    if summaries:
        # まず数を絞る（bondを優先）
        summaries = summaries[:1]
        trim_steps.append("SHARED_NARRATIVE件数削減")
        pack = assemble(
            capsule_lines=capsule_lines,
            facts=facts,
            summaries=summaries,
            relationship=relationship,
            loops=loops,
            evidence=evidence,
        )
        if len(pack) > max_chars:
            # 次に本文を短縮
            s0 = summaries[0]
            budget = max(120, min(600, max_chars // 3))
            if len(s0) > budget:
                summaries = [s0[: budget].rstrip() + "…"]
                trim_steps.append("SHARED_NARRATIVE本文短縮")
                pack = assemble(
                    capsule_lines=capsule_lines,
                    facts=facts,
                    summaries=summaries,
                    relationship=relationship,
                    loops=loops,
                    evidence=evidence,
                )
    if len(pack) <= max_chars:
        return _return_with_budget_warning(pack)

    # RelationshipState を落としても足りない場合は他の要素を削る。
    if relationship:
        relationship = []
        trim_steps.append("RELATIONSHIP_STATE削除")
        pack = assemble(
            capsule_lines=capsule_lines,
            facts=facts,
            summaries=summaries,
            relationship=relationship,
            loops=loops,
            evidence=evidence,
        )
    if len(pack) <= max_chars:
        return _return_with_budget_warning(pack)

    while facts and len(pack) > max_chars:
        facts = facts[:-1]
        if not trim_steps or not trim_steps[-1].startswith("STABLE_FACTS削減"):
            trim_steps.append("STABLE_FACTS削減")
        pack = assemble(
            capsule_lines=capsule_lines,
            facts=facts,
            summaries=summaries,
            relationship=relationship,
            loops=loops,
            evidence=evidence,
        )
    if len(pack) <= max_chars:
        return _return_with_budget_warning(pack)

    while capsule_lines and len(pack) > max_chars:
        capsule_lines = capsule_lines[:-1]
        if not trim_steps or not trim_steps[-1].startswith("CONTEXT_CAPSULE削減"):
            trim_steps.append("CONTEXT_CAPSULE削減")
        pack = assemble(
            capsule_lines=capsule_lines,
            facts=facts,
            summaries=summaries,
            relationship=relationship,
            loops=loops,
            evidence=evidence,
        )
    if len(pack) <= max_chars:
        return _return_with_budget_warning(pack)

    if max_chars <= 0:
        trim_steps.append("最終切り捨て(max_chars<=0)")
        return _return_with_budget_warning("")
    trim_steps.append("最終切り捨て")
    return _return_with_budget_warning(pack[: max(0, max_chars - 1)] + "\n")
