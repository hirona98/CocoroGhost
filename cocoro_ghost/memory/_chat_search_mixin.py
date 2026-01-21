"""
チャット（/api/chat）: 検索（候補収集/分散/pack生成）用のmixin。

目的:
    - `cocoro_ghost/memory/_chat_mixin.py` を肥大化させない。
    - 検索の「候補収集 → 偏り抑制 → 選別結果へ候補詳細を埋める（SearchResultPack）」を分離する。

NOTE:
    - ここは「返答ストリーム」ではなく、記憶検索・整形が責務。
    - mixin なので、`self.config_store` / `self.llm_client` / `self._log_retrieval_debug` が存在する前提。
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import math
import time
from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils, vector_index
from cocoro_ghost.db import memory_session_scope, search_similar_item_ids
from cocoro_ghost.llm_client import LlmRequestPurpose
from cocoro_ghost.memory._utils import now_utc_ts
from cocoro_ghost.memory_models import Event, EventAffect, EventLink, EventThread, State
from cocoro_ghost.time_utils import format_iso8601_local


@dataclass(frozen=True)
class _CandidateItem:
    """候補アイテム（イベント/状態/感情）。"""

    type: str  # event/state/event_affect
    id: int
    rank_ts: int
    meta: dict[str, Any]
    hit_sources: list[str]


def _parse_image_summaries_json(image_summaries_json: str | None) -> list[str]:
    """
    events.image_summaries_json を list[str] に正規化する。

    NOTE:
        - 画像要約は内部用だが、検索と返答整合性のために参照できるようにする。
        - JSONが壊れている場合は空として扱う（例外は投げない）。
    """
    return common_utils.parse_json_str_list(image_summaries_json)


def _fts_or_query(terms: list[str]) -> str:
    """
    FTS5 MATCH 用のORクエリを作る。

    目的:
        - 入力文字列に記号や空白が混ざっても検索が破綻しにくくする。
        - trigram FTS の「広め検索」を実現する。
    """
    cleaned: list[str] = []
    for t in terms:
        s = str(t or "").replace("\n", " ").replace("\r", " ").strip()
        if not s:
            continue
        if len(s) < 2:
            continue
        s = s.replace('"', '""')
        cleaned.append(f'"{s}"')
    if not cleaned:
        # NOTE: MATCH が空になるとエラーになる可能性があるため、最低限の語を返す。
        seed = str(terms[0] if terms else "_").replace("\n", " ").replace("\r", " ").strip()
        seed = seed.replace('"', '""')
        return f'"{seed}"'
    # NOTE: 長すぎるORは重くなるため、上位だけ使う。
    return " OR ".join(cleaned[:8])


class _ChatSearchMixin:
    """チャット検索（候補収集/分散/pack生成）の実装（mixin）。"""

    def _collect_candidates(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        event_id: int,
        input_text: str,
        plan_obj: dict[str, Any],
        vector_embedding_future: concurrent.futures.Future[list[Any]] | None = None,
    ) -> list[_CandidateItem]:
        """
        候補収集（取りこぼし防止優先・可能なものは並列）。

        Args:
            embedding_preset_id: 埋め込みプリセットID。
            embedding_dimension: 埋め込み次元。
            event_id: 現在ターンの event_id（除外用）。
            input_text: ユーザー入力。
            plan_obj: SearchPlan（文字n-gram/期間補助/上限制御などに使う）。
            vector_embedding_future:
                先行して開始した「input_text のみ」の埋め込み取得結果。
                - これが渡された場合、vector_all は原則この結果を使う（SearchPlanのqueriesは使わない）。
                - 目的: SearchPlan生成と埋め込み取得を重ねて体感を上げ、vec候補の欠落を減らす。
        """

        # --- 上限 ---
        limits = plan_obj.get("limits") if isinstance(plan_obj, dict) else None
        max_candidates = 200
        if isinstance(limits, dict) and isinstance(limits.get("max_candidates"), (int, float)):
            max_candidates = int(limits.get("max_candidates") or 200)
        max_candidates = max(1, min(400, max_candidates))

        # --- 並列候補収集（タイムアウトで全体が破綻しない） ---
        # NOTE:
        # - セッションはスレッドセーフではないため、各タスクで個別に開く。
        # - 遅い経路（特に embedding）があっても、全体を止めない。
        sources_by_key: dict[tuple[str, int], set[str]] = {}

        # --- 検索語（SearchPlan）を正として、複数クエリで広めに拾う ---
        queries_raw = plan_obj.get("queries") if isinstance(plan_obj, dict) else None
        query_texts: list[str] = [str(input_text or "").strip()]
        if isinstance(queries_raw, list):
            for q in queries_raw:
                qq = str(q or "").strip()
                if not qq:
                    continue
                if qq not in query_texts:
                    query_texts.append(qq)
        query_texts = [q for q in query_texts if q]

        # --- ベクトル検索は「input_text のみ」で先行埋め込みを使えるようにする ---
        # NOTE:
        # - 段階化（追加クエリの追い埋め込み）はしない（シンプル優先）。
        # - SearchPlan.queries は文字n-gram側の補助としては使えるが、vec側は input_text を正にする。
        vector_query_texts: list[str] = [str(input_text or "").strip()]
        vector_query_texts = [q for q in vector_query_texts if q]

        def add_sources(keys: list[tuple[str, int]], label: str) -> None:
            for t, i in keys:
                if not t or int(i) <= 0:
                    continue
                k = (str(t), int(i))
                s = sources_by_key.get(k)
                if s is None:
                    s = set()
                    sources_by_key[k] = s
                s.add(str(label))

        def task_recent_events() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = (
                    db.query(Event.event_id)
                    .filter(Event.searchable == 1)
                    .filter(Event.event_id != int(event_id))
                    .order_by(Event.created_at.desc(), Event.event_id.desc())
                    .limit(50)
                    .all()
                )
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_trigram_events() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                q = _fts_or_query(query_texts)
                rows = db.execute(
                    text(
                        """
                        SELECT f.rowid AS event_id
                        FROM events_fts AS f
                        JOIN events AS e ON e.event_id = f.rowid
                        WHERE f MATCH :q
                          AND f.rowid != :event_id
                          AND e.searchable = 1
                        ORDER BY f.rowid DESC
                        LIMIT 80
                        """
                    ),
                    {"q": str(q), "event_id": int(event_id)},
                ).fetchall()
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_reply_chain_events() -> list[tuple[str, int]]:
            # --- reply_to の連鎖を辿る（軽量な文脈復元） ---
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                cur = int(event_id)
                for _ in range(20):
                    link = (
                        db.query(EventLink)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link.to_event_id)
                    if prev_id <= 0:
                        break
                    out.append(("event", int(prev_id)))
                    cur = prev_id
            return out

        def task_context_threads_events() -> list[tuple[str, int]]:
            # --- 文脈スレッド（event_threads）から、同一文脈のイベントを拾う ---
            # NOTE:
            # - 現在ターン（event_id）は同期直後で thread 付与が無いことが多い。
            # - そのため reply_to 連鎖（直近の流れ）を「種」にして thread_key を集める。
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                # --- 種（seed）: reply_to 連鎖の先頭側（短め） ---
                seed_event_ids: list[int] = []
                cur = int(event_id)
                for _ in range(8):
                    link = (
                        db.query(EventLink.to_event_id)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link[0] or 0)
                    if prev_id <= 0:
                        break
                    seed_event_ids.append(int(prev_id))
                    cur = int(prev_id)

                if not seed_event_ids:
                    return []

                # --- thread_key を集める（重くしない） ---
                rows = (
                    db.query(EventThread.thread_key)
                    .filter(EventThread.event_id.in_([int(x) for x in seed_event_ids]))
                    .order_by(EventThread.id.desc())
                    .limit(16)
                    .all()
                )
                thread_keys: list[str] = []
                seen: set[str] = set()
                for r in rows:
                    tk = str(r[0] or "").strip()
                    if not tk:
                        continue
                    if tk in seen:
                        continue
                    seen.add(tk)
                    thread_keys.append(tk)

                if not thread_keys:
                    return []

                # --- 同一threadのイベントを拾う（最近順） ---
                rows2 = (
                    db.query(EventThread.event_id)
                    .filter(EventThread.thread_key.in_([str(x) for x in thread_keys]))
                    .order_by(EventThread.event_id.desc())
                    .limit(80)
                    .all()
                )
                for r in rows2:
                    if not r or r[0] is None:
                        continue
                    eid = int(r[0] or 0)
                    if eid <= 0 or int(eid) == int(event_id):
                        continue
                    out.append(("event", int(eid)))
            return out

        def task_context_links_events() -> list[tuple[str, int]]:
            # --- 文脈リンク（event_links）から、同一話題/因果/継続を拾う ---
            # NOTE:
            # - reply_to は別経路で辿るので、ここでは reply_to 以外を対象にする。
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                # --- 種（seed）: reply_to 連鎖の先頭側（短め） ---
                seed_event_ids: list[int] = []
                cur = int(event_id)
                for _ in range(8):
                    link = (
                        db.query(EventLink.to_event_id)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link[0] or 0)
                    if prev_id <= 0:
                        break
                    seed_event_ids.append(int(prev_id))
                    cur = int(prev_id)

                if not seed_event_ids:
                    return []

                seed_set = {int(x) for x in seed_event_ids if int(x) > 0}
                if not seed_set:
                    return []

                # --- 同一話題/因果/継続を拾う ---
                labels = ["same_topic", "caused_by", "continuation"]
                rows = (
                    db.query(EventLink.from_event_id, EventLink.to_event_id)
                    .filter(
                        (EventLink.from_event_id.in_([int(x) for x in seed_set]))
                        | (EventLink.to_event_id.in_([int(x) for x in seed_set]))
                    )
                    .filter(EventLink.label.in_([str(x) for x in labels]))
                    .order_by(EventLink.id.desc())
                    .limit(80)
                    .all()
                )
                for r in rows:
                    if not r:
                        continue
                    a = int(r[0] or 0)
                    b = int(r[1] or 0)
                    if a <= 0 or b <= 0:
                        continue
                    other = b if a in seed_set else a if b in seed_set else 0
                    if other <= 0 or int(other) == int(event_id):
                        continue
                    out.append(("event", int(other)))
            return out

        def task_recent_states() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = (
                    db.query(State.state_id)
                    .filter(State.searchable == 1)
                    .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                    .limit(40)
                    .all()
                )
                return [("state", int(r[0])) for r in rows if r and r[0] is not None]

        def task_about_time_events() -> list[tuple[str, int]]:
            # --- about_time / life_stage で拾う（全期間横断向け） ---
            mode = str(plan_obj.get("mode") or "").strip()
            time_hint = plan_obj.get("time_hint") if isinstance(plan_obj.get("time_hint"), dict) else {}
            y0 = time_hint.get("about_year_start")
            y1 = time_hint.get("about_year_end")
            life = str(time_hint.get("life_stage_hint") or "").strip()

            # NOTE: 明示ヒントが無いなら空で返す（ノイズを増やさない）
            if not y0 and not y1 and not life:
                return []

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                q = db.query(Event.event_id).filter(Event.searchable == 1)
                if y0 or y1:
                    ys = int(y0) if isinstance(y0, (int, float)) and int(y0) > 0 else None
                    ye = int(y1) if isinstance(y1, (int, float)) and int(y1) > 0 else None
                    if ys is not None and ye is None:
                        ye = ys
                    if ye is not None and ys is None:
                        ys = ye
                    if ys is not None and ye is not None:
                        q = q.filter(Event.about_year_start.isnot(None)).filter(Event.about_year_end.isnot(None))
                        q = q.filter(Event.about_year_start <= int(ye)).filter(Event.about_year_end >= int(ys))
                if life:
                    q = q.filter(Event.life_stage == str(life))
                limit = 120 if mode == "explicit_about_time" else 80
                rows = q.order_by(Event.created_at.desc(), Event.event_id.desc()).limit(int(limit)).all()
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_vector_all() -> tuple[list[tuple[str, int]], dict[str, Any]]:
            # --- 類似検索の件数（k）を設定から決める ---
            # NOTE:
            # - embedding_preset.similar_episodes_limit は「類似イベント（episodes）の上限」を表す。
            # - 今回は vec 側は input_text を正とし、総量が暴れないように per-query に割り当てる。
            # - state / event_affect は従来の比率（60:40:20）を踏襲し、events を基準に派生させる。
            cfg = self.config_store.config  # type: ignore[attr-defined]
            total_event_k = max(1, min(200, int(cfg.similar_episodes_limit)))
            qn = max(1, len(vector_query_texts))

            per_query_event_k = int(math.ceil(float(total_event_k) / float(qn)))
            total_state_k = int(math.ceil(float(total_event_k) * (2.0 / 3.0)))
            total_affect_k = int(math.ceil(float(total_event_k) * (1.0 / 3.0)))
            per_query_state_k = max(1, int(math.ceil(float(total_state_k) / float(qn))))
            per_query_affect_k = max(1, int(math.ceil(float(total_affect_k) / float(qn))))

            # --- embedding を用意する（重いので、可能なら先行開始した結果を使う） ---
            # NOTE:
            # - 先行埋め込みがあれば、SearchPlan生成（同期）と埋め込み取得を重ねて待ちを削る。
            # - 先行埋め込みが無い場合のみ、ここで取得する。
            embeddings: list[Any]
            embedding_source: str = "generated_in_vector_all"
            if vector_embedding_future is not None:
                # --- vec_all タスク内での待ち時間を抑える（SQLite検索の時間を確保） ---
                budget_seconds = 2.0
                started = time.perf_counter()
                remaining = max(0.0, float(budget_seconds) - float(time.perf_counter() - started))
                try:
                    embeddings = vector_embedding_future.result(timeout=float(remaining))
                    embedding_source = "precomputed_input_only"
                except concurrent.futures.TimeoutError:
                    # NOTE:
                    # - 先行埋め込みは「体感速度改善」のための最適化であり、失敗しても検索自体は継続する。
                    # - ここで例外を投げるとチャット応答が返らなくなるため、同期で取り直す。
                    embeddings = self.llm_client.generate_embedding(  # type: ignore[attr-defined]
                        [str(x) for x in vector_query_texts],
                        purpose=LlmRequestPurpose.SYNC_RETRIEVAL_QUERY_EMBEDDING,
                    )
                    embedding_source = "generated_after_precompute_timeout"
                except Exception:  # noqa: BLE001
                    # NOTE: 予期しない失敗でも、検索を止めずに同期で取り直す。
                    embeddings = self.llm_client.generate_embedding(  # type: ignore[attr-defined]
                        [str(x) for x in vector_query_texts],
                        purpose=LlmRequestPurpose.SYNC_RETRIEVAL_QUERY_EMBEDDING,
                    )
                    embedding_source = "generated_after_precompute_error"
            else:
                embeddings = self.llm_client.generate_embedding(  # type: ignore[attr-defined]
                    [str(x) for x in vector_query_texts],
                    purpose=LlmRequestPurpose.SYNC_RETRIEVAL_QUERY_EMBEDDING,
                )

            # --- mode によって最近性フィルタを使う ---
            mode = str(plan_obj.get("mode") or "").strip()
            rank_range = None
            if mode == "associative_recent":
                today_day = int(now_utc_ts()) // 86400
                rank_range = (int(today_day) - 90, int(today_day) + 1)

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                out: list[tuple[str, int]] = []
                dbg: dict[str, Any] = {
                    "embedding_preset_id": str(embedding_preset_id),
                    "memory_enabled": bool(self.config_store.memory_enabled),  # type: ignore[attr-defined]
                    "mode": str(mode),
                    "rank_day_range": (list(rank_range) if rank_range is not None else None),
                    "vector_query_texts": list(vector_query_texts),
                    "trigram_query_texts": list(query_texts),
                    "embedding_source": str(embedding_source),
                    "similar_episodes_limit": int(total_event_k),
                    "k_per_query": {
                        "event": int(per_query_event_k),
                        "state": int(per_query_state_k),
                        "event_affect": int(per_query_affect_k),
                    },
                    "vec_items_counts": {},
                    "hits": {"event": [], "state": [], "event_affect": []},
                }

                # --- vec_items の状況（育っていない時の診断用） ---
                # NOTE: vec_items が空だと search_similar_item_ids の結果も空になりやすい。
                try:
                    total = db.execute(text("SELECT COUNT(*) FROM vec_items")).scalar()
                    c_event = db.execute(
                        text("SELECT COUNT(*) FROM vec_items WHERE kind=:k"),
                        {"k": int(vector_index.VEC_KIND_EVENT)},
                    ).scalar()
                    c_state = db.execute(
                        text("SELECT COUNT(*) FROM vec_items WHERE kind=:k"),
                        {"k": int(vector_index.VEC_KIND_STATE)},
                    ).scalar()
                    c_aff = db.execute(
                        text("SELECT COUNT(*) FROM vec_items WHERE kind=:k"),
                        {"k": int(vector_index.VEC_KIND_EVENT_AFFECT)},
                    ).scalar()
                    dbg["vec_items_counts"] = {
                        "total": int(total or 0),
                        "event": int(c_event or 0),
                        "state": int(c_state or 0),
                        "event_affect": int(c_aff or 0),
                    }
                except Exception:  # noqa: BLE001
                    dbg["vec_items_counts"] = {"error": "count failed"}

                # --- 各クエリ埋め込みで拾ったヒットを統合する ---
                for q_text, q_emb in zip(vector_query_texts, embeddings, strict=False):
                    q_label = str(q_text)[:60]

                    rows_e = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_event_k),
                        kind=int(vector_index.VEC_KIND_EVENT),
                        rank_day_range=rank_range,
                        active_only=True,
                    )
                    for r in rows_e:
                        if not r or r[0] is None:
                            continue
                        item_id = int(r[0])
                        distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                        event_id2 = vector_index.vec_entity_id(item_id)
                        if int(event_id2) == int(event_id):
                            continue
                        out.append(("event", int(event_id2)))
                        dbg["hits"]["event"].append(
                            {"event_id": int(event_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                    rows_s = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_state_k),
                        kind=int(vector_index.VEC_KIND_STATE),
                        rank_day_range=None,
                        active_only=True,
                    )
                    for r in rows_s:
                        if not r or r[0] is None:
                            continue
                        item_id = int(r[0])
                        distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                        state_id2 = vector_index.vec_entity_id(item_id)
                        out.append(("state", int(state_id2)))
                        dbg["hits"]["state"].append(
                            {"state_id": int(state_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                    rows_a = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_affect_k),
                        kind=int(vector_index.VEC_KIND_EVENT_AFFECT),
                        rank_day_range=None,
                        active_only=True,
                    )
                    for r in rows_a:
                        if not r or r[0] is None:
                            continue
                        item_id = int(r[0])
                        distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                        affect_id2 = vector_index.vec_entity_id(item_id)
                        out.append(("event_affect", int(affect_id2)))
                        dbg["hits"]["event_affect"].append(
                            {"affect_id": int(affect_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                return out, dbg

        # --- 並列実行（遅い経路があっても全体が破綻しない） ---
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                "recent_events": ex.submit(task_recent_events),
                "trigram_events": ex.submit(task_trigram_events),
                "reply_chain": ex.submit(task_reply_chain_events),
                "context_threads": ex.submit(task_context_threads_events),
                "context_links": ex.submit(task_context_links_events),
                "recent_states": ex.submit(task_recent_states),
                "about_time": ex.submit(task_about_time_events),
                "vector_all": ex.submit(task_vector_all),
            }

            timeouts = {
                "recent_events": 0.25,
                "trigram_events": 0.6,
                "reply_chain": 0.2,
                "context_threads": 0.35,
                "context_links": 0.35,
                "recent_states": 0.25,
                "about_time": 0.4,
                "vector_all": 2.2,
            }

            vector_debug: dict[str, Any] | None = None
            for label, fut in futures.items():
                try:
                    result = fut.result(timeout=float(timeouts.get(label, 0.5)))
                    if label == "vector_all":
                        keys, vector_debug = result
                    else:
                        keys = result
                    add_sources([(str(t), int(i)) for (t, i) in keys], label=str(label))
                except Exception as exc:  # noqa: BLE001
                    if label == "vector_all":
                        vector_debug = {"error": f"vector_all failed or timed out: {type(exc).__name__}: {exc}"}
                    continue

        # --- レコードをまとめて引く（ORMのDetachedを避けるため、候補のdict化までセッション内で行う） ---
        keys_all = sorted([k for k in sources_by_key.keys() if not (k[0] == "event" and int(k[1]) == int(event_id))])
        if not keys_all:
            return []

        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            event_ids = [int(i) for (t, i) in keys_all if t == "event"]
            state_ids = [int(i) for (t, i) in keys_all if t == "state"]
            affect_ids = [int(i) for (t, i) in keys_all if t == "event_affect"]

            events = (
                db.query(Event).filter(Event.searchable == 1).filter(Event.event_id.in_(event_ids)).all()
                if event_ids
                else []
            )
            states = (
                db.query(State).filter(State.searchable == 1).filter(State.state_id.in_(state_ids)).all()
                if state_ids
                else []
            )
            affects = (
                db.query(EventAffect)
                .join(Event, Event.event_id == EventAffect.event_id)
                .filter(Event.searchable == 1)
                .filter(EventAffect.id.in_(affect_ids))
                .all()
                if affect_ids
                else []
            )

            by_event_id = {int(r.event_id): r for r in events}
            by_state_id = {int(r.state_id): r for r in states}
            by_affect_id = {int(r.id): r for r in affects}

            # --- event_threads（文脈スレッド）をイベントメタへ添える ---
            # NOTE:
            # - thread_key は「なんの流れだっけ？」のヒントとして有効。
            # - 同期の選別入力へ含めることで、選別の安定性を上げる。
            threads_by_event_id: dict[int, list[str]] = {}
            if event_ids:
                rows = (
                    db.query(EventThread.event_id, EventThread.thread_key)
                    .filter(EventThread.event_id.in_([int(x) for x in event_ids]))
                    .order_by(EventThread.id.desc())
                    .all()
                )
                for r in rows:
                    if not r:
                        continue
                    eid = int(r[0] or 0)
                    tk = str(r[1] or "").strip()
                    if eid <= 0 or not tk:
                        continue
                    lst = threads_by_event_id.get(eid)
                    if lst is None:
                        lst = []
                        threads_by_event_id[eid] = lst
                    if tk not in lst:
                        lst.append(tk)

            affect_event_ids = sorted({int(a.event_id) for a in affects if a and a.event_id is not None})
            affect_events = (
                db.query(Event).filter(Event.searchable == 1).filter(Event.event_id.in_(affect_event_ids)).all()
                if affect_event_ids
                else []
            )
            by_affect_event_id = {int(r.event_id): r for r in affect_events}

            out: list[_CandidateItem] = []
            for t, i in keys_all:
                hit_sources = sorted(list(sources_by_key.get((t, int(i))) or set()))

                if t == "event":
                    r = by_event_id.get(int(i))
                    if r is None:
                        continue
                    # --- 画像要約（内部用）を候補メタへ添える（サイズを抑えたプレビュー） ---
                    img_summaries = _parse_image_summaries_json(getattr(r, "image_summaries_json", None))
                    img_preview = "\n".join([x for x in img_summaries if str(x or "").strip()]).strip()
                    if img_preview and len(img_preview) > 800:
                        img_preview = img_preview[:800]
                    out.append(
                        _CandidateItem(
                            type="event",
                            id=int(i),
                            rank_ts=int(r.created_at),
                            meta={
                                "type": "event",
                                "event_id": int(r.event_id),
                                "created_at": format_iso8601_local(int(r.created_at)),
                                "source": str(r.source),
                                "thread_keys": threads_by_event_id.get(int(r.event_id), []),
                                "user_text": str(r.user_text or "")[:800],
                                "assistant_text": str(r.assistant_text or "")[:800],
                                "image_summaries_preview": (str(img_preview) if img_preview else None),
                                "about_time": {
                                    "about_year_start": r.about_year_start,
                                    "about_year_end": r.about_year_end,
                                    "life_stage": r.life_stage,
                                    "confidence": float(r.about_time_confidence),
                                },
                            },
                            hit_sources=hit_sources,
                        )
                    )
                elif t == "state":
                    s = by_state_id.get(int(i))
                    if s is None:
                        continue
                    # --- long_mood_state は背景として別途注入するため、候補（SearchResultPack）には入れない ---
                    if str(s.kind) == "long_mood_state":
                        continue
                    out.append(
                        _CandidateItem(
                            type="state",
                            id=int(i),
                            rank_ts=int(s.last_confirmed_at),
                            meta={
                                "type": "state",
                                "state_id": int(s.state_id),
                                "kind": str(s.kind),
                                "body_text": str(s.body_text)[:900],
                                "payload_json": str(s.payload_json)[:1200],
                                "last_confirmed_at": format_iso8601_local(int(s.last_confirmed_at)),
                                "valid_from_ts": (
                                    format_iso8601_local(int(s.valid_from_ts)) if s.valid_from_ts is not None else None
                                ),
                                "valid_to_ts": (
                                    format_iso8601_local(int(s.valid_to_ts)) if s.valid_to_ts is not None else None
                                ),
                            },
                            hit_sources=hit_sources,
                        )
                    )
                elif t == "event_affect":
                    a = by_affect_id.get(int(i))
                    if a is None:
                        continue
                    ev2 = by_affect_event_id.get(int(a.event_id))
                    event_created_at = int(ev2.created_at) if ev2 is not None else None
                    out.append(
                        _CandidateItem(
                            type="event_affect",
                            id=int(i),
                            rank_ts=int(a.created_at),
                            meta={
                                "type": "event_affect",
                                "affect_id": int(a.id),
                                "event_id": int(a.event_id),
                                "created_at": format_iso8601_local(int(a.created_at)),
                                "event_created_at": (
                                    format_iso8601_local(int(event_created_at)) if event_created_at is not None else None
                                ),
                                "moment_affect_text": common_utils.strip_face_tags(str(a.moment_affect_text or ""))[:600],
                                "moment_affect_labels": common_utils.parse_json_str_list(getattr(a, "moment_affect_labels_json", None))[:6],
                                "inner_thought_text": (
                                    common_utils.strip_face_tags(str(a.inner_thought_text))[:600]
                                    if a.inner_thought_text is not None
                                    else None
                                ),
                                "vad": {"v": float(a.vad_v), "a": float(a.vad_a), "d": float(a.vad_d)},
                                "confidence": float(a.confidence),
                            },
                            hit_sources=hit_sources,
                        )
                    )

        # --- デバッグ: 埋め込みDB（vec_items）由来の候補を表示する ---
        # NOTE:
        # - ここはLLMへ投げる情報の一部なので、LLM I/O ログ（DEBUG）へ揃えて出す。
        # - vector_all の経路でヒットした候補を中心に表示する（取りこぼしと重複の確認用）。
        vector_loaded_preview = [c.meta for c in out if "vector_all" in (c.hit_sources or [])][:30]
        if vector_debug is not None:
            self._log_retrieval_debug(  # type: ignore[attr-defined]
                "（（埋め込みDBから候補取得））",
                {
                    "query_text": str(input_text),
                    "trigram_query_texts": list(query_texts),
                    "vector_query_texts": list(vector_query_texts),
                    "mode": str(plan_obj.get("mode") or ""),
                    "vector_debug": vector_debug,
                    "loaded_preview": vector_loaded_preview,
                },
            )

        out = sorted(out, key=lambda x: (len(x.hit_sources), int(x.rank_ts), int(x.id)), reverse=True)

        # --- diversify（候補分散）: event候補の偏りを抑える ---
        # NOTE:
        # - mode=targeted_broad/explicit_about_time のときだけ適用する（associative_recentは最近性優先）。
        # - 現状は event にしか about_time（life_stage/about_year）が無いため、eventのみ対象にする。
        out = self._apply_diversify_inplace(candidates=out, plan_obj=plan_obj)

        return out[:max_candidates]

    def _apply_diversify_inplace(self, *, candidates: list[_CandidateItem], plan_obj: dict[str, Any]) -> list[_CandidateItem]:
        """
        SearchPlan.diversify に基づき、event候補の並び順だけを調整する。

        目的:
            - max_candidates で切り詰める前に「偏り」を抑え、LLM選別の入力を安定させる。

        方針:
            - mode=targeted_broad/explicit_about_time の場合のみ適用する。
            - event 候補のみを対象にし、state/event_affect の順序と件数は変えない。
            - まずは per_bucket の上限内で広く拾い、残りは元の順位順で詰める（取りこぼし優先）。
        """

        # --- plan/diversify を読む ---
        if not isinstance(plan_obj, dict):
            return candidates

        mode = str(plan_obj.get("mode") or "").strip()
        if mode not in ("targeted_broad", "explicit_about_time"):
            return candidates

        diversify = plan_obj.get("diversify")
        if not isinstance(diversify, dict):
            return candidates

        by_raw = diversify.get("by")
        by_list = [str(x or "").strip() for x in (by_raw if isinstance(by_raw, list) else [])]
        by_list = [x for x in by_list if x]
        if not by_list:
            return candidates

        # --- サポートする軸だけに絞る ---
        supported = {"life_stage", "about_year_bucket"}
        by_keys = [k for k in by_list if k in supported]
        if not by_keys:
            return candidates

        per_bucket_raw = diversify.get("per_bucket")
        per_bucket = int(per_bucket_raw) if isinstance(per_bucket_raw, (int, float)) else 5
        per_bucket = max(1, min(20, int(per_bucket)))

        # --- event候補を抽出（元の順序） ---
        events_only: list[_CandidateItem] = [c for c in candidates if str(c.type) == "event"]
        if len(events_only) <= 1:
            return candidates

        # --- バケット値を計算する（event.meta から） ---
        def bucket_life_stage(ev: _CandidateItem) -> str:
            # --- about_time から取得 ---
            about = ev.meta.get("about_time") if isinstance(ev.meta, dict) else None
            if not isinstance(about, dict):
                return "unknown"
            v = str(about.get("life_stage") or "").strip()
            allowed = {"elementary", "middle", "high", "university", "work", "unknown"}
            return v if v in allowed else "unknown"

        def bucket_about_year_bucket(ev: _CandidateItem) -> str:
            # --- about_time から取得 ---
            about = ev.meta.get("about_time") if isinstance(ev.meta, dict) else None
            if not isinstance(about, dict):
                return "unknown"
            y0 = about.get("about_year_start")
            y1 = about.get("about_year_end")

            # --- 年を正規化 ---
            ys = int(y0) if isinstance(y0, (int, float)) and int(y0) > 0 else None
            ye = int(y1) if isinstance(y1, (int, float)) and int(y1) > 0 else None
            if ys is None and ye is None:
                return "unknown"
            year = ys if ye is None else ye if ys is None else int((int(ys) + int(ye)) // 2)
            if year <= 0 or year > 9999:
                return "unknown"

            # --- 5年刻みバケット（例: 2018 -> 2015-2019） ---
            start = int(year) - (int(year) % 5)
            end = int(start) + 4
            return f"{start}-{end}"

        def buckets(ev: _CandidateItem) -> dict[str, str]:
            out_b: dict[str, str] = {}
            for k in by_keys:
                if k == "life_stage":
                    out_b[k] = bucket_life_stage(ev)
                elif k == "about_year_bucket":
                    out_b[k] = bucket_about_year_bucket(ev)
            return out_b

        # --- 第一パス: 上限内で広く拾う ---
        counts_by_key: dict[str, dict[str, int]] = {k: {} for k in by_keys}
        selected: list[_CandidateItem] = []
        deferred: list[_CandidateItem] = []

        for ev in events_only:
            b = buckets(ev)
            ok = True
            for k, bv in b.items():
                cur = counts_by_key[k].get(bv, 0)
                if int(cur) >= int(per_bucket):
                    ok = False
                    break
            if ok:
                # --- 採用 ---
                selected.append(ev)
                for k, bv in b.items():
                    counts_by_key[k][bv] = int(counts_by_key[k].get(bv, 0)) + 1
            else:
                # --- いったん保留 ---
                deferred.append(ev)

        # --- 第二パス: 残りを順位順で詰める ---
        selected.extend(deferred)

        # --- eventの位置だけ差し替えて返す（state/event_affectはそのまま） ---
        it = iter(selected)
        rebuilt: list[_CandidateItem] = []
        for c in candidates:
            if str(c.type) == "event":
                rebuilt.append(next(it))
            else:
                rebuilt.append(c)
        return rebuilt

    def _inflate_search_result_pack(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        candidates: list[_CandidateItem],
        pack: dict[str, Any],
    ) -> dict[str, Any]:
        """
        選別結果に候補詳細を埋め、返答生成へ渡しやすい形へ整形する。

        画像付きチャット対応:
            - events.image_summaries_json（画像要約）を「選別済みのイベント」にだけ付与する。
              （候補全体へ付けると入力が肥大化しやすいため）
        """

        # --- 入力を正規化 ---
        if not isinstance(pack, dict):
            return {"selected": []}

        selected = pack.get("selected") if isinstance(pack.get("selected"), list) else []

        # --- 候補辞書（type:id -> meta） ---
        by_key: dict[str, dict[str, Any]] = {}
        for c in candidates:
            key = f"{str(c.type)}:{int(c.id)}"
            by_key[key] = dict(c.meta) | {"hit_sources": list(c.hit_sources)}

        out_selected: list[dict[str, Any]] = []
        for s in selected:
            if not isinstance(s, dict):
                continue

            # --- type が無い場合は event_id の有無で推定する（LLMの揺れ吸収） ---
            t = str(s.get("type") or "").strip()
            if not t:
                if int(s.get("event_id") or 0) > 0:
                    t = "event"
                elif int(s.get("state_id") or 0) > 0:
                    t = "state"
                elif int(s.get("affect_id") or 0) > 0:
                    t = "event_affect"
                else:
                    continue

            # --- typeごとにキーを決める ---
            key: str | None = None
            if t == "event":
                eid = int(s.get("event_id") or 0)
                if eid > 0:
                    key = f"event:{eid}"
            elif t == "state":
                sid = int(s.get("state_id") or 0)
                if sid > 0:
                    key = f"state:{sid}"
            elif t == "event_affect":
                aid = int(s.get("affect_id") or 0)
                if aid > 0:
                    key = f"event_affect:{aid}"

            if key is None:
                continue
            item = by_key.get(key)
            if item is None:
                continue

            out_selected.append(
                {
                    "type": str(t),
                    "why": str(s.get("why") or "").strip(),
                    "snippet": str(s.get("snippet") or "").strip(),
                    "item": item,
                }
            )

        # --- 選別済みイベントへ、画像要約（詳細）を追記する（内部用） ---
        # NOTE:
        # - SearchResultPack で「過去の画像要約」を参照できるようにする。
        # - 候補全体に付けるとトークンが膨らみやすいので、選別済みだけに限定する。
        event_ids = sorted({int(x.get("item", {}).get("event_id") or 0) for x in out_selected if x.get("type") == "event"})
        event_ids = [int(x) for x in event_ids if int(x) > 0]
        if event_ids:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = db.query(Event.event_id, Event.image_summaries_json).filter(
                    Event.event_id.in_([int(x) for x in event_ids])
                ).all()
            summaries_by_event_id: dict[int, list[str]] = {}
            for r in rows:
                if not r:
                    continue
                eid = int(r[0] or 0)
                if eid <= 0:
                    continue
                img_json = r[1] if len(r) > 1 else None
                summaries_by_event_id[eid] = _parse_image_summaries_json(img_json)
            for x in out_selected:
                if x.get("type") != "event":
                    continue
                item2 = x.get("item") if isinstance(x.get("item"), dict) else None
                if item2 is None:
                    continue
                eid = int(item2.get("event_id") or 0)
                if eid <= 0:
                    continue
                item2["image_summaries"] = summaries_by_event_id.get(int(eid), [])

        return {"selected": out_selected}
