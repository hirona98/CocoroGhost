"""
Workerジョブハンドラ（整理・リンク系）

役割:
- state_links 構築
- tidy_memory 実行
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost.db import memory_session_scope, search_similar_item_ids
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import Event, Job, Revision, State, StateLink
from cocoro_ghost.time_utils import format_iso8601_local
from cocoro_ghost.worker_constants import (
    JOB_PENDING as _JOB_PENDING,
    TIDY_ACTIVE_STATE_FETCH_LIMIT as _TIDY_ACTIVE_STATE_FETCH_LIMIT,
)
from cocoro_ghost.worker_handlers_common import (
    _canonicalize_json_for_dedupe,
    _normalize_text_for_dedupe,
    _now_utc_ts,
    _state_link_row_to_json,
    _state_row_to_json,
)


def _handle_build_state_links(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """
    state↔state のリンク（state_links）を生成して保存する。

    目的:
        - state は「育つノート」なので、関連/派生/矛盾/補足のリンクを少数・高品質で残す。
        - 同期検索では `state_link_expand` で辿って候補を増やし、取りこぼしを減らす。

    方針:
        - 候補収集: ベクトル近傍（state）で k 件だけ拾い、距離で足切りする。
        - 判定: LLMで「リンクする/しない + label + confidence」を返させる。
        - 保存: Unique(from,to,label) で upsert し、必ず revisions を残す。
        - 失敗: このジョブだけが失敗する（WritePlan適用は止めない）。
    """

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    state_ids_raw = payload.get("state_ids")
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")
    if not isinstance(state_ids_raw, list):
        raise RuntimeError("payload.state_ids is required (list[int])")

    state_ids = [int(x) for x in state_ids_raw if isinstance(x, (int, float)) and int(x) > 0]
    state_ids = sorted({int(x) for x in state_ids if int(x) > 0}, reverse=True)
    if not state_ids:
        return

    # --- 設定（TOML）を読む ---
    # NOTE: 初期化順や例外の影響を避けるため、取れない場合は安全寄りの既定値へフォールバックする。
    try:
        from cocoro_ghost.config import get_config_store

        cfg = get_config_store().config
        enabled = bool(getattr(cfg, "memory_state_links_build_enabled", True))
        candidate_k = int(getattr(cfg, "memory_state_links_build_candidate_k", 24))
        max_links_per_state = int(getattr(cfg, "memory_state_links_build_max_links_per_state", 6))
        min_conf = float(getattr(cfg, "memory_state_links_build_min_confidence", 0.65))
        max_distance = float(getattr(cfg, "memory_state_links_build_max_distance", 0.35))
    except Exception:  # noqa: BLE001
        enabled = True
        candidate_k = 24
        max_links_per_state = 6
        min_conf = 0.65
        max_distance = 0.35

    if not enabled:
        return

    candidate_k = int(candidate_k)
    max_links_per_state = int(max_links_per_state)
    min_conf = float(min_conf)
    max_distance = float(max_distance)

    now_ts = _now_utc_ts()

    # --- base_state ごとにリンク生成 ---
    # NOTE:
    # - ここは「少数・高品質」が目的なので、1stateあたり max_links_per_state を厳守する。
    # - long_mood_state は背景であり、リンク対象から除外する。
    #
    # NOTE:
    # - SQLAlchemy の ORM オブジェクトはセッション外に持ち出すと expired/detached になりやすい。
    # - ここはジョブであり安定性を最優先し、DBから読むものは「スナップショット(dict)」に変換して扱う。
    def _payload_preview(payload_json: str) -> str:
        s = str(payload_json or "").strip()
        if not s:
            return ""
        if len(s) > 600:
            return s[:600] + "…"
        return s

    for base_state_id in state_ids:
        if max_links_per_state <= 0:
            continue

        # --- base_state をロードしてスナップショット化（セッション外でORMを触らない） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            base = db.query(State).filter(State.searchable == 1).filter(State.state_id == int(base_state_id)).one_or_none()
            if base is None:
                continue
            if str(getattr(base, "kind", "")) == "long_mood_state":
                continue

            base_text_for_emb = _build_state_embedding_text(base)
            if not base_text_for_emb:
                continue

            base_snapshot = {
                "state_id": int(base.state_id),
                "kind": str(base.kind),
                "body_text": str(base.body_text),
                "payload_preview": _payload_preview(str(base.payload_json)),
                "valid_from_ts": (int(base.valid_from_ts) if base.valid_from_ts is not None else None),
                "valid_to_ts": (int(base.valid_to_ts) if base.valid_to_ts is not None else None),
                "last_confirmed_at": int(base.last_confirmed_at),
            }

        # --- 候補（candidate_states）をベクトル近傍から集める ---
        # NOTE:
        # - base_state 自体の vec_items が未更新でも検索できるよう、クエリ埋め込みは都度生成する。
        # - active_only=True で「現役のstate」を優先し、ノイズを抑える。
        q_emb = llm_client.generate_embedding([base_text_for_emb], purpose=LlmRequestPurpose.ASYNC_STATE_LINKS)[0]

        # --- candidate_states をロードしてスナップショット化 ---
        candidate_snapshots: list[dict[str, Any]] = []
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rows = search_similar_item_ids(
                db,
                query_embedding=q_emb,
                k=int(candidate_k),
                kind=int(vector_index.VEC_KIND_STATE),
                rank_day_range=None,
                active_only=True,
            )

            # --- vec_items -> state_id へ変換し、距離で足切り ---
            candidate_state_ids: list[int] = []
            for r in rows:
                if not r or r[0] is None:
                    continue
                item_id = int(r[0])
                distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                if distance is not None and float(distance) > float(max_distance):
                    continue
                st_id = int(vector_index.vec_entity_id(int(item_id)))
                if st_id <= 0:
                    continue
                if st_id == int(base_snapshot["state_id"]):
                    continue
                if st_id not in candidate_state_ids:
                    candidate_state_ids.append(int(st_id))

            if not candidate_state_ids:
                continue

            # --- candidate_states をロード（検索対象のみ + long_mood_state除外） ---
            cand_states2 = (
                db.query(State)
                .filter(State.searchable == 1)
                .filter(State.state_id.in_([int(x) for x in candidate_state_ids]))
                .filter(State.kind != "long_mood_state")
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .all()
            )
            for s in cand_states2:
                if int(s.state_id) == int(base_snapshot["state_id"]):
                    continue
                candidate_snapshots.append(
                    {
                        "state_id": int(s.state_id),
                        "kind": str(s.kind),
                        "body_text": str(s.body_text),
                        "payload_preview": _payload_preview(str(s.payload_json)),
                        "valid_from_ts": (int(s.valid_from_ts) if s.valid_from_ts is not None else None),
                        "valid_to_ts": (int(s.valid_to_ts) if s.valid_to_ts is not None else None),
                        "last_confirmed_at": int(s.last_confirmed_at),
                    }
                )

        if not candidate_snapshots:
            continue

        # --- LLM入力を構築（JSON文字列） ---
        # NOTE:
        # - ここは Worker 内部用。プロンプトは system 側で固定し、入力は機械的なJSONにする。
        input_obj = {"base_state": dict(base_snapshot), "candidate_states": list(candidate_snapshots)}
        input_text = common_utils.json_dumps(input_obj)

        # --- LLMでリンクを判定（JSONのみ） ---
        resp = llm_client.generate_json_response(
            system_prompt=prompt_builders.state_links_system_prompt(),
            input_text=input_text,
            purpose=LlmRequestPurpose.ASYNC_STATE_LINKS,
            max_tokens=400,
        )
        obj = common_utils.parse_first_json_object_or_none(common_utils.first_choice_content(resp)) or {}
        links_raw = obj.get("links")
        if not isinstance(links_raw, list):
            continue

        # --- 出力を正規化（採用/足切り） ---
        allowed_labels = {"relates_to", "derived_from", "supports", "contradicts"}
        candidate_id_set = {int(s.get("state_id") or 0) for s in candidate_snapshots}

        normalized: list[dict[str, Any]] = []
        for x in links_raw:
            if not isinstance(x, dict):
                continue
            to_state_id = int(x.get("to_state_id") or 0)
            if to_state_id <= 0:
                continue
            if to_state_id == int(base_snapshot["state_id"]):
                continue
            if to_state_id not in candidate_id_set:
                continue

            label = str(x.get("label") or "").strip()
            if label not in allowed_labels:
                continue

            conf = float(x.get("confidence") or 0.0)
            if conf < float(min_conf):
                continue

            why = str(x.get("why") or "").strip()
            if len(why) > 240:
                why = why[:240].rstrip() + "…"

            normalized.append(
                {
                    "to_state_id": int(to_state_id),
                    "label": str(label),
                    "confidence": float(conf),
                    "why": str(why),
                }
            )

        # --- 0件なら保存しない ---
        if not normalized:
            continue

        # --- 上限をかける（max_links_per_state） ---
        normalized.sort(key=lambda d: float(d.get("confidence") or 0.0), reverse=True)
        normalized = normalized[: int(max_links_per_state)]

        # --- DBへ upsert + revision ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- revisions を追加するヘルパ（リンク生成でも説明責任を残す） ---
            def add_revision(
                *,
                entity_type: str,
                entity_id: int,
                before: Any,
                after: Any,
                reason: str,
                evidence_event_ids: list[int],
            ) -> None:
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

            # --- evidence_event_ids を安定化 ---
            evidence_ids = sorted({int(event_id)})

            # --- 観測用の件数 ---
            created_links = 0
            updated_links = 0

            def upsert_one_link(
                *,
                from_state_id: int,
                to_state_id: int,
                label: str,
                confidence: float,
                why: str,
                evidence_event_ids_in: list[int],
                reverse: bool,
            ) -> None:
                """state_links を1本 upsert し、revision を残す。"""

                nonlocal created_links, updated_links

                existing = (
                    db.query(StateLink)
                    .filter(StateLink.from_state_id == int(from_state_id))
                    .filter(StateLink.to_state_id == int(to_state_id))
                    .filter(StateLink.label == str(label))
                    .one_or_none()
                )

                # --- reason 文字列を整形 ---
                rev_tag = "（逆向き）" if bool(reverse) else ""
                why2 = str(why or "").strip()
                reason_add = f"state_links を追加した{rev_tag}: {why2}" if why2 else f"state_links を追加した{rev_tag}"
                reason_upd = f"state_links を更新した{rev_tag}: {why2}" if why2 else f"state_links を更新した{rev_tag}"

                if existing is None:
                    link = StateLink(
                        from_state_id=int(from_state_id),
                        to_state_id=int(to_state_id),
                        label=str(label),
                        confidence=float(confidence),
                        evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_event_ids_in if int(x) > 0]),
                        created_at=int(now_ts),
                    )
                    db.add(link)
                    db.flush()
                    add_revision(
                        entity_type="state_links",
                        entity_id=int(link.id),
                        before=None,
                        after=_state_link_row_to_json(link),
                        reason=str(reason_add),
                        evidence_event_ids=[int(x) for x in evidence_event_ids_in if int(x) > 0],
                    )
                    created_links += 1
                    return

                before = _state_link_row_to_json(existing)

                # --- evidence_event_ids をマージ（重複なし） ---
                prev_obj = common_utils.json_loads_maybe(str(existing.evidence_event_ids_json or "[]"))
                prev_ids = (
                    [int(x) for x in prev_obj if isinstance(prev_obj, list) and isinstance(x, (int, float))]
                    if isinstance(prev_obj, list)
                    else []
                )
                merged_ids = sorted({int(x) for x in (prev_ids + list(evidence_event_ids_in)) if int(x) > 0})
                existing.confidence = float(confidence)
                existing.evidence_event_ids_json = common_utils.json_dumps(merged_ids)
                db.add(existing)
                db.flush()
                add_revision(
                    entity_type="state_links",
                    entity_id=int(existing.id),
                    before=before,
                    after=_state_link_row_to_json(existing),
                    reason=str(reason_upd),
                    evidence_event_ids=list(merged_ids),
                )
                updated_links += 1

            # --- 対称関係は「逆向き」も保存する ---
            # NOTE:
            # - 現状の label セットには逆関係（supported_by 等）が無い。
            # - そのため、自動で逆向きを張って良いのは「対称」と見なせる関係だけに限定する。
            # - relates_to / contradicts は対称として扱えるため、両方向に保存する。
            symmetric_labels = {"relates_to", "contradicts"}

            for ln in normalized:
                to_state_id = int(ln["to_state_id"])
                label = str(ln["label"])
                conf = float(ln["confidence"])
                why = str(ln.get("why") or "").strip()

                # --- 正方向（base -> to） ---
                upsert_one_link(
                    from_state_id=int(base_snapshot["state_id"]),
                    to_state_id=int(to_state_id),
                    label=str(label),
                    confidence=float(conf),
                    why=str(why),
                    evidence_event_ids_in=list(evidence_ids),
                    reverse=False,
                )

                # --- 逆方向（to -> base、対称関係のみ） ---
                if str(label) in symmetric_labels:
                    upsert_one_link(
                        from_state_id=int(to_state_id),
                        to_state_id=int(base_snapshot["state_id"]),
                        label=str(label),
                        confidence=float(conf),
                        why=str(why),
                        evidence_event_ids_in=list(evidence_ids),
                        reverse=True,
                    )

        # --- 観測ログ（件数のみ） ---
        logger.info(
            "build_state_links done base_state_id=%s created=%s updated=%s candidates=%s",
            int(base_snapshot["state_id"]),
            int(created_links),
            int(updated_links),
            int(len(candidate_snapshots)),
            extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
        )


def _handle_tidy_memory(*, embedding_preset_id: str, embedding_dimension: int, payload: dict[str, Any]) -> None:
    """記憶整理（削除せず、集約/過去化でノイズを減らす）。"""

    now_ts = _now_utc_ts()
    max_close = int(payload.get("max_close_per_run") or _TIDY_MAX_CLOSE_PER_RUN)
    max_close = int(max_close)

    # --- 変更件数（観測用） ---
    closed_state_ids: list[int] = []

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- revisions を追加するヘルパ（整理でも説明責任を残す） ---
        def add_revision(*, entity_type: str, entity_id: int, before: Any, after: Any, reason: str) -> None:
            db.add(
                Revision(
                    entity_type=str(entity_type),
                    entity_id=int(entity_id),
                    before_json=(common_utils.json_dumps(before) if before is not None else None),
                    after_json=(common_utils.json_dumps(after) if after is not None else None),
                    reason=str(reason or "").strip() or "(no reason)",
                    evidence_event_ids_json="[]",
                    created_at=int(now_ts),
                )
            )

        # --- 完全一致の重複（kind/body/payload）を過去化する ---
        # NOTE:
        # - 「近い意味」の統合は品質難度が高いので、まずは安全な完全一致だけ整理する
        # - 候補収集は広めを正とするため、ここは「ノイズの上限」を抑える役割
        # - long_mood_state は整理対象から除外する（単一更新で育てるのを正とする）
        active_states = (
            db.query(State)
            .filter(State.valid_to_ts.is_(None))
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .limit(int(_TIDY_ACTIVE_STATE_FETCH_LIMIT))
            .all()
        )

        seen_keys: set[str] = set()
        for st in active_states:
            if len(closed_state_ids) >= int(max_close):
                break

            # --- long_mood_state は整理対象外 ---
            if str(st.kind) == "long_mood_state":
                continue

            key = "|".join(
                [
                    str(st.kind),
                    _normalize_text_for_dedupe(str(st.body_text)),
                    _canonicalize_json_for_dedupe(str(st.payload_json)),
                ]
            )
            if key in seen_keys:
                before = _state_row_to_json(st)
                # NOTE: 最近性（last_confirmed_at）は弄らない（検索順位を壊さない）
                st.valid_to_ts = int(now_ts)
                st.updated_at = int(now_ts)
                db.add(st)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(st.state_id),
                    before=before,
                    after=_state_row_to_json(st),
                    reason="記憶整理: 完全一致の重複stateを過去化した",
                )
                closed_state_ids.append(int(st.state_id))
                continue

            seen_keys.add(key)

        # --- 3) 過去化したstateの埋め込みを更新する（activeフラグを反映） ---
        for state_id in closed_state_ids:
            db.add(
                Job(
                    kind="upsert_state_embedding",
                    payload_json=common_utils.json_dumps({"state_id": int(state_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    # --- 観測ログ（件数のみ） ---
    logger.info(
        "tidy_memory done closed_states=%s",
        int(len(closed_state_ids)),
        extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
    )


