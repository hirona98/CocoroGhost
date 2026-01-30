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

from sqlalchemy import func, text

from cocoro_ghost import common_utils, vector_index
from cocoro_ghost.db import memory_session_scope, search_similar_item_ids
from cocoro_ghost.llm_client import LlmRequestPurpose
from cocoro_ghost.memory._utils import now_utc_ts
from cocoro_ghost.memory_models import Event, EventAffect, EventEntity, EventLink, EventThread, State, StateEntity, StateLink
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

    def _apply_source_quota(
        self,
        *,
        candidates: list[_CandidateItem],
        plan_obj: dict[str, Any],
        max_candidates: int,
    ) -> list[_CandidateItem]:
        """
        候補の「経路偏り」を抑えるため、TOML設定の割合配分で候補を間引く。

        目的:
            - trigram / context / vector など「増えやすい経路」が席を独占すると、選別入力が膨らみ体感が悪化する。
            - 一方で品質（取りこぼし）を落とさないため、multi-hit（複数経路一致）と state を優先的に残す。

        方針:
            - まず候補をカテゴリに分ける（「何を残したいか」を明示して、偏りを抑える）。
              - state: 事実/タスクなど。会話の安定性に効くため、経路より優先して確保する。
              - multi-hit: 複数経路で一致した候補（hit_sources>=2）。取りこぼし防止に効くため厚めに確保する。
              - *-only: hit_sourcesが1件の候補を「どの経路だけで当たったか」で分類する。
                - vector-only: 語一致しないが意味が近いケースの拾い上げに効く
                - context-only: 返信連鎖/同一文脈/因果など「流れ」維持に効く
                - trigram-only: 固有名詞/型番/部分一致に効くが、ノイズも増えやすい
                - recent-only: 直近の連想に効くが、弱い一致が増えやすい
                - about_time-only: 年/ライフステージのヒント検索（全期間モード向け）
              - affect: event_affect（瞬間感情）。会話トーン補助だが、候補を食い過ぎやすいので「最大割合」だけ設ける。
            - mode に応じて、retrieval_max_candidates（=N）のX%を各カテゴリへ割り当てる（TOMLで設定）。
            - X%枠が埋まらない場合は「次カテゴリへ繰り越し」し、最終的にN件を確保する（不足は元の順位順で埋める）。
        """

        # --- 入力の正規化 ---
        if not candidates:
            return []
        cap = max(1, int(max_candidates))
        cap = max(1, min(400, int(cap)))

        # --- mode に応じた割合設定を取得する ---
        # NOTE:
        # - associative_recent は「直近の連想」が中心なので multi-hit / vector-only / context-only を厚めにする。
        # - targeted_broad/explicit_about_time は「全期間」なので about_time-only を厚めにする。
        mode = str(plan_obj.get("mode") or "").strip()
        tc = self.config_store.toml_config  # type: ignore[attr-defined]
        if mode in ("targeted_broad", "explicit_about_time"):
            # --- 全期間モード向けの配分 ---
            # NOTE:
            # - multi-hit/state を確保しつつ、期間ヒント（about_time-only）を厚めにする。
            # - context-only は「今の流れ」寄りなので薄め。
            quota_percent = {
                "state": int(tc.retrieval_quota_broad_state_percent),
                "multi_hit": int(tc.retrieval_quota_broad_multi_hit_percent),
                "about_time_only": int(tc.retrieval_quota_broad_about_time_only_percent),
                "vector_global_only": int(tc.retrieval_explore_global_vector_percent),
                "vector_only": int(tc.retrieval_quota_broad_vector_only_percent),
                "context_only": int(tc.retrieval_quota_broad_context_only_percent),
                # NOTE: entity_expand の単独hit。固有名詞寄りなので少量だけ確保する。
                "entity_only": int(getattr(tc, "retrieval_quota_broad_entity_only_percent", 5)),
                "trigram_only": int(tc.retrieval_quota_broad_trigram_only_percent),
            }
            category_order = [
                "state",
                "multi_hit",
                "about_time_only",
                "vector_global_only",
                "vector_only",
                "context_only",
                "entity_only",
                "trigram_only",
            ]
        else:
            # --- 直近連想モード向けの配分 ---
            # NOTE:
            # - multi-hit/state を確保しつつ、意味近傍（vector-only）と流れ（context-only）を厚めにする。
            # - trigram-only/recent-only は増えやすい割にノイズも増えるため薄め。
            quota_percent = {
                "state": int(tc.retrieval_quota_assoc_state_percent),
                "multi_hit": int(tc.retrieval_quota_assoc_multi_hit_percent),
                "vector_global_only": int(tc.retrieval_explore_global_vector_percent),
                "vector_only": int(tc.retrieval_quota_assoc_vector_only_percent),
                "context_only": int(tc.retrieval_quota_assoc_context_only_percent),
                # NOTE: entity_expand の単独hit。固有名詞寄りなので少量だけ確保する。
                "entity_only": int(getattr(tc, "retrieval_quota_assoc_entity_only_percent", 5)),
                "trigram_only": int(tc.retrieval_quota_assoc_trigram_only_percent),
                "recent_only": int(tc.retrieval_quota_assoc_recent_only_percent),
            }
            category_order = [
                "state",
                "multi_hit",
                "vector_global_only",
                "vector_only",
                "context_only",
                "entity_only",
                "trigram_only",
                "recent_only",
            ]

        # --- event_affect の最大割合（上限） ---
        # NOTE:
        # - event_affect は「トーン調整」の補助。候補を食い過ぎると本筋（state/event）の候補が減りやすい。
        # - ここは「最大割合」で縛るだけ（= 上限キャップ）。無理に最低件数を確保するとノイズが混ざりやすいので行わない。
        #   - 例: retrieval_max_candidates=60, retrieval_event_affect_max_percent=5 の場合、event_affect は最大3件まで。
        #   - 0% にすると、event_affect は候補に入らない（= トーン調整を候補で行わない）。
        affect_max_percent = max(0, min(100, int(tc.retrieval_event_affect_max_percent)))
        affect_cap = int(math.floor(float(cap) * (float(affect_max_percent) / 100.0)))
        affect_cap = max(0, min(int(cap), int(affect_cap)))

        # --- カテゴリ判定 ---
        # NOTE:
        # - state / event_affect は「種別が重要」なので、経路に関係なく先に分ける。
        # - multi-hit は複数経路一致（len(hit_sources)>=2）。経路の信頼度を直接比較しない代わりに、
        #   「複数の根拠がある候補」を優先して残し、取りこぼしを抑える。
        # - *-only は hit_sources が1件の候補を「どの経路だけで当たったか」で分類する。
        #   これにより、量が増えやすい経路が候補を独占するのを抑えられる。
        def category_of(c: _CandidateItem) -> str:
            if str(c.type) == "state":
                return "state"
            if str(c.type) == "event_affect":
                return "affect"
            hs = list(c.hit_sources or [])
            if len(hs) >= 2:
                return "multi_hit"
            if len(hs) == 1:
                src = str(hs[0] or "").strip()
                if src == "vector_recent":
                    return "vector_only"
                if src == "vector_global":
                    return "vector_global_only"
                if src == "about_time":
                    return "about_time_only"
                if src == "trigram_events":
                    return "trigram_only"
                if src == "recent_events":
                    return "recent_only"
                if src == "entity_expand":
                    return "entity_only"
                if src in ("reply_chain", "context_threads", "context_links"):
                    # NOTE:
                    # - reply_chain: 返信連鎖（直前文脈）
                    # - context_threads: 同一スレッド（話題の流れ）
                    # - context_links: same_topic/caused_by/continuation（話題/因果/継続）
                    return "context_only"
            return "other"

        # --- カテゴリごとに候補を分配（元の順序を保持） ---
        by_cat: dict[str, list[_CandidateItem]] = {}
        for c in candidates:
            k = category_of(c)
            lst = by_cat.get(k)
            if lst is None:
                lst = []
                by_cat[k] = lst
            lst.append(c)

        # --- 割合（%）→ 上限件数へ変換 ---
        # NOTE:
        # - 端数は floor で落とす（例: N=80, 5% → 4件）。
        # - 割合の合計が100%未満でもOK。残りは後段で埋めて取りこぼしを抑える。
        # - 逆に100%ちょうどでも「そのカテゴリに候補が無い」場合があるため、繰り越しが重要。
        quotas: dict[str, int] = {}
        for k, pct in quota_percent.items():
            p = max(0, min(100, int(pct)))
            quotas[str(k)] = int(math.floor(float(cap) * (float(p) / 100.0)))

        # --- 選抜（カテゴリ枠 → 余りは順位順で埋める） ---
        selected: list[_CandidateItem] = []
        selected_keys: set[str] = set()
        selected_affects = 0

        def try_add(item: _CandidateItem) -> bool:
            nonlocal selected_affects
            if len(selected) >= int(cap):
                return False
            key = f"{str(item.type)}:{int(item.id)}"
            if key in selected_keys:
                return False
            # --- affect 上限 ---
            if str(item.type) == "event_affect":
                if int(affect_cap) <= 0:
                    return False
                if int(selected_affects) >= int(affect_cap):
                    return False
                selected_affects += 1
            selected.append(item)
            selected_keys.add(key)
            return True

        # --- 1) カテゴリ枠で採用（余りは次カテゴリへ繰り越す） ---
        # NOTE:
        # - 「X%を厳密固定」すると、候補が薄いカテゴリがある場合に総数が不足しやすい。
        # - ここでは「枠の余り」を carry して次カテゴリへ回し、候補総数を安定させる（品質優先）。
        carry = 0
        for cat in category_order:
            base_limit = int(quotas.get(str(cat), 0))
            limit = int(base_limit) + int(carry)
            if limit <= 0:
                carry = int(carry) + int(base_limit)
                continue
            pool = by_cat.get(str(cat), [])
            used = 0
            for item in pool:
                if used >= int(limit):
                    break
                if try_add(item):
                    used += 1
            carry = int(limit) - int(used)

        # --- 2) 余りをカテゴリ優先順で埋める（soft quota） ---
        # NOTE:
        # - ここは「不足分を埋める」段階。
        # - まずはカテゴリ優先順（state/multi-hit/...）で追加し、偏りを抑えたまま総数を確保する。
        # - affect は上限を維持したまま埋める。
        if len(selected) < int(cap):
            for cat in list(category_order) + ["other"]:
                for item in by_cat.get(str(cat), []):
                    if len(selected) >= int(cap):
                        break
                    try_add(item)

        # --- 3) 最終フォールバック: 元の順位順で埋める ---
        # NOTE: どのカテゴリにも属さない候補が多い場合でも、総数を確保して取りこぼしを減らす。
        if len(selected) < int(cap):
            for item in candidates:
                if len(selected) >= int(cap):
                    break
                try_add(item)

        return selected

    def _collect_candidates(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        event_id: int,
        input_text: str,
        plan_obj: dict[str, Any],
        vector_embedding_future: concurrent.futures.Future[list[Any]] | None = None,
    ) -> tuple[list[_CandidateItem], dict[str, Any]]:
        """
        候補収集（取りこぼし防止優先・可能なものは並列）。

        Args:
            embedding_preset_id: 埋め込みプリセットID。
            embedding_dimension: 埋め込み次元。
            event_id: 現在ターンの event_id（除外用）。
            input_text: ユーザー入力。
            plan_obj: RetrievalPlan（SearchPlan互換dict。文字n-gram/期間補助/上限制御などに使う）。
            vector_embedding_future:
                先行して開始した「input_text のみ」の埋め込み取得結果。
                - これが渡された場合、vec検索は原則この結果を使う（vec側は input_text を正とする）。
                - 目的: SSE開始までの体感を上げ、vec候補の欠落を減らす。
        """

        # --- 収集デバッグ（観測用。retrieval_runs に残す用途） ---
        # NOTE:
        # - ここは「ログ」だけでなく、retrieval_runs.candidates_json にも残して後から追えるようにする。
        # - 値はJSON化される前提なので、シリアライズ可能な形に限定する。
        collect_debug: dict[str, Any] = {}

        # --- 上限 ---
        limits = plan_obj.get("limits") if isinstance(plan_obj, dict) else None
        max_candidates = 200
        if isinstance(limits, dict) and isinstance(limits.get("max_candidates"), (int, float)):
            max_candidates = int(limits.get("max_candidates") or 200)
        max_candidates = max(1, min(400, max_candidates))

        # --- 起動設定（TOML）の上限を強制する ---
        # NOTE:
        # - plan_obj は同一プロセス内のルール生成だが、運用時の安全弁としてTOML上限を最終適用する。
        # - 体感速度（選別入力の膨張）を守るため、ここは常に強制する。
        toml_max_candidates = int(self.config_store.toml_config.retrieval_max_candidates)  # type: ignore[attr-defined]
        toml_max_candidates = max(1, min(400, int(toml_max_candidates)))
        max_candidates = int(min(int(max_candidates), int(toml_max_candidates)))

        # --- 並列候補収集（タイムアウトで全体が破綻しない） ---
        # NOTE:
        # - セッションはスレッドセーフではないため、各タスクで個別に開く。
        # - 遅い経路（特に embedding）があっても、全体を止めない。
        sources_by_key: dict[tuple[str, int], set[str]] = {}

        # --- 検索語（plan_obj.queries）を正として、複数クエリで広めに拾う ---
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
        # - plan_obj.queries は文字n-gram側の補助としては使えるが、vec側は input_text を正にする。
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

        def task_vector_recent_global() -> tuple[dict[str, list[tuple[str, int]]], dict[str, Any]]:
            # --- 類似検索の件数（k）を設定から決める ---
            # NOTE:
            # - embedding_preset.similar_episodes_limit は「類似イベント（episodes）の上限」を表す。
            # - 今回は vec 側は input_text を正とし、総量が暴れないように per-query に割り当てる。
            # - state / event_affect は従来の比率（60:40:20）を踏襲し、events を基準に派生させる。
            cfg = self.config_store.config  # type: ignore[attr-defined]
            tc = self.config_store.toml_config  # type: ignore[attr-defined]
            total_event_k = max(1, min(200, int(cfg.similar_episodes_limit)))
            qn = max(1, len(vector_query_texts))

            per_query_event_k = int(math.ceil(float(total_event_k) / float(qn)))
            total_state_k = int(math.ceil(float(total_event_k) * (2.0 / 3.0)))
            total_affect_k = int(math.ceil(float(total_event_k) * (1.0 / 3.0)))
            per_query_state_k = max(1, int(math.ceil(float(total_state_k) / float(qn))))
            per_query_affect_k = max(1, int(math.ceil(float(total_affect_k) / float(qn))))

            # --- 「ひらめき枠」用の global vec(event) を少数だけ拾う ---
            # NOTE:
            # - 目的は「毎ターン、少数だけ全期間から混ぜる」ことで、会話の着想を得やすくすること。
            # - 最終的な混入割合は quota（TOML）で制御するため、ここでは「候補プール」を適度に確保するだけ。
            # - 0% にした場合は探索を完全に止め、無駄な検索を行わない（速度/安定性優先）。
            explore_percent = max(0, min(100, int(tc.retrieval_explore_global_vector_percent)))
            explore_cap = int(math.floor(float(max_candidates) * (float(explore_percent) / 100.0)))
            global_event_k_total = int(min(80, max(0, int(explore_cap) * 6)))
            per_query_global_event_k = (
                max(1, int(math.ceil(float(global_event_k_total) / float(qn)))) if global_event_k_total > 0 else 0
            )

            # --- embedding を用意する（重いので、可能なら先行開始した結果を使う） ---
            # NOTE:
            # - 先行埋め込みがあれば、SSE開始前の他処理と埋め込み取得を重ねて待ちを削る。
            # - 先行埋め込みが無い場合のみ、ここで取得する。
            embeddings: list[Any]
            embedding_source: str = "generated_in_vector_task"
            if vector_embedding_future is not None:
                # --- vec タスク内での待ち時間を抑える（SQLite検索の時間を確保） ---
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

            # --- 最近性フィルタ（eventのみ） ---
            # NOTE:
            # - 毎ターン「最近の連想」は会話の自然さに効くため、eventは常に recent と global を分けて拾う。
            # - 期間指定（explicit_about_time）のときでも recent を混ぜるが、最終配分は quota が抑制する。
            mode = str(plan_obj.get("mode") or "").strip()
            today_day = int(now_utc_ts()) // 86400
            recent_rank_range = (int(today_day) - 90, int(today_day) + 1)

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                out_recent: list[tuple[str, int]] = []
                out_global: list[tuple[str, int]] = []
                out_states: list[tuple[str, int]] = []
                out_affects: list[tuple[str, int]] = []

                recent_event_ids: set[int] = set()
                global_event_ids: set[int] = set()
                state_ids: set[int] = set()
                affect_ids: set[int] = set()

                dbg: dict[str, Any] = {
                    "embedding_preset_id": str(embedding_preset_id),
                    "memory_enabled": bool(self.config_store.memory_enabled),  # type: ignore[attr-defined]
                    "mode": str(mode),
                    "recent_rank_day_range": list(recent_rank_range),
                    "vector_query_texts": list(vector_query_texts),
                    "trigram_query_texts": list(query_texts),
                    "embedding_source": str(embedding_source),
                    "similar_episodes_limit": int(total_event_k),
                    "k_per_query": {
                        "event_recent": int(per_query_event_k),
                        "event_global": int(per_query_global_event_k),
                        "state": int(per_query_state_k),
                        "event_affect": int(per_query_affect_k),
                    },
                    "vec_items_counts": {},
                    "hits": {"event_recent": [], "event_global": [], "state": [], "event_affect": []},
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

                    # --- event（recent） ---
                    rows_e = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_event_k),
                        kind=int(vector_index.VEC_KIND_EVENT),
                        rank_day_range=recent_rank_range,
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
                        if int(event_id2) in recent_event_ids:
                            continue
                        recent_event_ids.add(int(event_id2))
                        out_recent.append(("event", int(event_id2)))
                        dbg["hits"]["event_recent"].append(
                            {"event_id": int(event_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                    # --- event（global / ひらめき枠） ---
                    if int(per_query_global_event_k) > 0:
                        rows_eg = search_similar_item_ids(
                            db,
                            query_embedding=q_emb,
                            k=int(per_query_global_event_k),
                            kind=int(vector_index.VEC_KIND_EVENT),
                            rank_day_range=None,
                            active_only=True,
                        )
                        for r in rows_eg:
                            if not r or r[0] is None:
                                continue
                            item_id = int(r[0])
                            distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                            event_id2 = vector_index.vec_entity_id(item_id)
                            if int(event_id2) == int(event_id):
                                continue
                            # NOTE: recentと重複するなら「ひらめき枠」の意味が薄いので除外する。
                            if int(event_id2) in recent_event_ids:
                                continue
                            if int(event_id2) in global_event_ids:
                                continue
                            global_event_ids.add(int(event_id2))
                            out_global.append(("event", int(event_id2)))
                            dbg["hits"]["event_global"].append(
                                {"event_id": int(event_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                            )

                    # --- state ---
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
                        if int(state_id2) in state_ids:
                            continue
                        state_ids.add(int(state_id2))
                        out_states.append(("state", int(state_id2)))
                        dbg["hits"]["state"].append(
                            {"state_id": int(state_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                    # --- event_affect ---
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
                        if int(affect_id2) in affect_ids:
                            continue
                        affect_ids.add(int(affect_id2))
                        out_affects.append(("event_affect", int(affect_id2)))
                        dbg["hits"]["event_affect"].append(
                            {"affect_id": int(affect_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                # --- 結果をラベル別に返す（hit_sourcesに反映する） ---
                # NOTE:
                # - state/event_affect は種別が優先されるため、vec由来であることだけ分かればよい。
                # - eventは「recent」と「global（ひらめき枠）」を分け、quotaで混入割合を制御する。
                out_by_label: dict[str, list[tuple[str, int]]] = {
                    "vector_recent": list(out_recent) + list(out_states) + list(out_affects),
                    "vector_global": list(out_global),
                }
                return out_by_label, dbg

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
                "vector": ex.submit(task_vector_recent_global),
            }

            timeouts = {
                "recent_events": 0.25,
                "trigram_events": 0.6,
                "reply_chain": 0.2,
                "context_threads": 0.35,
                "context_links": 0.35,
                "recent_states": 0.25,
                "about_time": 0.4,
                "vector": 2.2,
            }

            vector_debug: dict[str, Any] | None = None
            for label, fut in futures.items():
                try:
                    result = fut.result(timeout=float(timeouts.get(label, 0.5)))
                    if label == "vector":
                        by_label, vector_debug = result
                        for sub_label, keys in (by_label or {}).items():
                            add_sources([(str(t), int(i)) for (t, i) in (keys or [])], label=str(sub_label))
                    else:
                        keys = result
                        add_sources([(str(t), int(i)) for (t, i) in keys], label=str(label))
                except Exception as exc:  # noqa: BLE001
                    if label == "vector":
                        vector_debug = {"error": f"vector task failed or timed out: {type(exc).__name__}: {exc}"}
                    continue

        # --- state_link_expand（seed → state_links → 関連state） ---
        # NOTE:
        # - state は「育つノート」なので、state同士のリンク（state_links）を辿ると取りこぼしが減る。
        # - 多段化（リンク→リンク→…）はノイズ/コストが増えるため、ここでは1-hopのみ。
        tc = self.config_store.toml_config  # type: ignore[attr-defined]
        state_link_expand_debug: dict[str, Any] = {
            "enabled": bool(getattr(tc, "retrieval_state_link_expand_enabled", True)),
            "seed_state_ids": [],
            "added_state_count": 0,
        }
        if bool(state_link_expand_debug["enabled"]):
            seed_limit = max(0, int(getattr(tc, "retrieval_state_link_expand_seed_limit", 8)))
            per_seed_limit = max(0, int(getattr(tc, "retrieval_state_link_expand_per_seed_limit", 8)))
            min_conf = float(getattr(tc, "retrieval_state_link_expand_min_confidence", 0.6))
            min_conf = max(0.0, min(1.0, float(min_conf)))
            state_link_expand_debug["limits"] = {
                "seed_limit": int(seed_limit),
                "per_seed_limit": int(per_seed_limit),
                "min_confidence": float(min_conf),
            }

            # --- seed（state候補）を選ぶ（multi-hit優先 + 最近性っぽくID降順） ---
            seed_state_ids: list[int] = []
            for k, srcs in sources_by_key.items():
                if not k or len(k) != 2:
                    continue
                t, i = k
                if str(t) != "state":
                    continue
                if int(i) <= 0:
                    continue
                # NOTE: 既に展開で付いたseedは除外し、1-hopのままにする（多段化を避ける）
                if "entity_expand" in (srcs or set()):
                    continue
                if "state_link_expand" in (srcs or set()):
                    continue
                seed_state_ids.append(int(i))

            def seed_score(state_id: int) -> tuple[int, int]:
                hs = sources_by_key.get(("state", int(state_id))) or set()
                return (int(len(hs)), int(state_id))

            seed_state_ids.sort(key=seed_score, reverse=True)
            seed_state_ids = seed_state_ids[: max(0, int(seed_limit))]
            state_link_expand_debug["seed_state_ids"] = [int(x) for x in seed_state_ids]

            # --- seed が無い/上限ゼロなら何もしない ---
            if seed_state_ids and int(per_seed_limit) > 0:
                added_ids: set[int] = set()
                with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                    for seed_state_id in seed_state_ids:
                        # --- seed -> to（順方向） ---
                        rows_fwd = (
                            db.query(StateLink.to_state_id)
                            .join(State, State.state_id == StateLink.to_state_id)
                            .filter(StateLink.from_state_id == int(seed_state_id))
                            .filter(StateLink.confidence >= float(min_conf))
                            .filter(State.searchable == 1)
                            .filter(State.kind != "long_mood_state")
                            .order_by(StateLink.confidence.desc(), StateLink.id.desc())
                            .limit(int(per_seed_limit))
                            .all()
                        )
                        keys_fwd = [("state", int(r[0])) for r in rows_fwd if r and r[0] is not None]
                        for _t, sid in keys_fwd:
                            if int(sid) == int(seed_state_id):
                                continue
                            added_ids.add(int(sid))
                        add_sources(keys_fwd, label="state_link_expand")

                        # --- seed <- from（逆方向。seedがto側のリンクも辿る） ---
                        rows_rev = (
                            db.query(StateLink.from_state_id)
                            .join(State, State.state_id == StateLink.from_state_id)
                            .filter(StateLink.to_state_id == int(seed_state_id))
                            .filter(StateLink.confidence >= float(min_conf))
                            .filter(State.searchable == 1)
                            .filter(State.kind != "long_mood_state")
                            .order_by(StateLink.confidence.desc(), StateLink.id.desc())
                            .limit(int(per_seed_limit))
                            .all()
                        )
                        keys_rev = [("state", int(r[0])) for r in rows_rev if r and r[0] is not None]
                        for _t, sid in keys_rev:
                            if int(sid) == int(seed_state_id):
                                continue
                            added_ids.add(int(sid))
                        add_sources(keys_rev, label="state_link_expand")

                state_link_expand_debug["added_state_count"] = int(len({int(x) for x in added_ids if int(x) > 0}))

        collect_debug["state_link_expand"] = state_link_expand_debug

        # --- entity_expand（seed → entity → 関連候補） ---
        # NOTE:
        # - AutoMemの expand_entities 相当の「多段想起」を、同期候補収集へ最小実装で入れる。
        # - 既存の検索（recent/trigram/context/vector）で拾った候補を seed にして、
        #   そこに付与された entity索引から関連イベント/状態を追加で引く。
        tc = self.config_store.toml_config  # type: ignore[attr-defined]
        entity_expand_debug: dict[str, Any] = {
            "enabled": bool(getattr(tc, "retrieval_entity_expand_enabled", True)),
            "seed_keys": [],
            "selected_entities": [],
            "added_keys": {"event": 0, "state": 0},
        }
        if bool(entity_expand_debug["enabled"]):
            # --- 起動設定（上限と足切り） ---
            seed_limit = max(0, int(getattr(tc, "retrieval_entity_expand_seed_limit", 12)))
            max_entities = max(0, int(getattr(tc, "retrieval_entity_expand_max_entities", 12)))
            per_entity_event_limit = max(0, int(getattr(tc, "retrieval_entity_expand_per_entity_event_limit", 10)))
            per_entity_state_limit = max(0, int(getattr(tc, "retrieval_entity_expand_per_entity_state_limit", 6)))
            min_conf = float(getattr(tc, "retrieval_entity_expand_min_confidence", 0.45))
            min_conf = max(0.0, min(1.0, float(min_conf)))
            min_seed_occ = max(1, int(getattr(tc, "retrieval_entity_expand_min_seed_occurrences", 2)))
            entity_expand_debug["limits"] = {
                "seed_limit": int(seed_limit),
                "max_entities": int(max_entities),
                "per_entity_event_limit": int(per_entity_event_limit),
                "per_entity_state_limit": int(per_entity_state_limit),
                "min_confidence": float(min_conf),
                "min_seed_occurrences": int(min_seed_occ),
            }

            # --- seed の選定（multi-hit優先 + 最近性っぽくID降順） ---
            # NOTE:
            # - ここは「候補の一部」を seed にするだけで、品質は後段のLLM選別に任せる。
            seed_keys: list[tuple[str, int]] = []
            for k, srcs in sources_by_key.items():
                if not k or len(k) != 2:
                    continue
                t, i = k
                if str(t) not in ("event", "state"):
                    continue
                if int(i) <= 0:
                    continue
                seed_keys.append((str(t), int(i)))

            def seed_score(key: tuple[str, int]) -> tuple[int, int]:
                # --- multi-hit優先（経路が多いほど強いseedとみなす） ---
                hs = sources_by_key.get(key) or set()
                return (int(len(hs)), int(key[1]))

            seed_keys.sort(key=seed_score, reverse=True)
            seed_keys = seed_keys[: max(0, int(seed_limit))]
            entity_expand_debug["seed_keys"] = [f"{t}:{int(i)}" for (t, i) in seed_keys]

            # --- seed が無いなら何もしない ---
            if seed_keys and max_entities > 0 and (per_entity_event_limit > 0 or per_entity_state_limit > 0):
                with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                    seed_event_ids = [int(i) for (t, i) in seed_keys if t == "event"]
                    seed_state_ids = [int(i) for (t, i) in seed_keys if t == "state"]

                    # --- seed から entity を集約（出現回数 + 最大confidence） ---
                    stats: dict[tuple[str, str], dict[str, Any]] = {}

                    # events
                    if seed_event_ids:
                        rows = (
                            db.query(
                                EventEntity.entity_type_norm,
                                EventEntity.entity_name_norm,
                                func.max(EventEntity.confidence),
                                func.count(EventEntity.id),
                            )
                            .filter(EventEntity.event_id.in_([int(x) for x in seed_event_ids]))
                            .group_by(EventEntity.entity_type_norm, EventEntity.entity_name_norm)
                            .all()
                        )
                        for r in rows:
                            if not r:
                                continue
                            t_norm = str(r[0] or "").strip()
                            name_norm = str(r[1] or "").strip()
                            conf = float(r[2] or 0.0)
                            cnt = int(r[3] or 0)
                            if not t_norm or not name_norm:
                                continue
                            if conf < float(min_conf):
                                continue
                            if int(cnt) < int(min_seed_occ):
                                continue
                            k = (t_norm, name_norm)
                            cur = stats.get(k)
                            if cur is None:
                                stats[k] = {"cnt": int(cnt), "conf": float(conf)}
                            else:
                                cur["cnt"] = int(cur.get("cnt") or 0) + int(cnt)
                                cur["conf"] = max(float(cur.get("conf") or 0.0), float(conf))

                    # states
                    if seed_state_ids:
                        rows = (
                            db.query(
                                StateEntity.entity_type_norm,
                                StateEntity.entity_name_norm,
                                func.max(StateEntity.confidence),
                                func.count(StateEntity.id),
                            )
                            .filter(StateEntity.state_id.in_([int(x) for x in seed_state_ids]))
                            .group_by(StateEntity.entity_type_norm, StateEntity.entity_name_norm)
                            .all()
                        )
                        for r in rows:
                            if not r:
                                continue
                            t_norm = str(r[0] or "").strip()
                            name_norm = str(r[1] or "").strip()
                            conf = float(r[2] or 0.0)
                            cnt = int(r[3] or 0)
                            if not t_norm or not name_norm:
                                continue
                            if conf < float(min_conf):
                                continue
                            if int(cnt) < int(min_seed_occ):
                                continue
                            k = (t_norm, name_norm)
                            cur = stats.get(k)
                            if cur is None:
                                stats[k] = {"cnt": int(cnt), "conf": float(conf)}
                            else:
                                cur["cnt"] = int(cur.get("cnt") or 0) + int(cnt)
                                cur["conf"] = max(float(cur.get("conf") or 0.0), float(conf))

                    # --- entity を「型ごと」に分けて、ラウンドロビンで多様化 ---
                    # NOTE: 1種に偏るとノイズが増えやすいので、シンプルな分散を入れる。
                    type_order = ["person", "org", "place", "project", "tool"]
                    by_type: dict[str, list[tuple[str, float]]] = {t: [] for t in type_order}
                    for (t_norm, name_norm), v in stats.items():
                        if t_norm not in by_type:
                            continue
                        score = float((int(v.get("cnt") or 0) * 2)) + float(v.get("conf") or 0.0)
                        by_type[t_norm].append((name_norm, score))
                    for t_norm in type_order:
                        by_type[t_norm].sort(key=lambda x: float(x[1]), reverse=True)

                    selected_entities: list[tuple[str, str]] = []
                    while len(selected_entities) < int(max_entities):
                        progressed = False
                        for t_norm in type_order:
                            pool = by_type.get(t_norm) or []
                            if not pool:
                                continue
                            name_norm, _score = pool.pop(0)
                            selected_entities.append((str(t_norm), str(name_norm)))
                            progressed = True
                            if len(selected_entities) >= int(max_entities):
                                break
                        if not progressed:
                            break

                    # --- デバッグ用: 選んだentity（seed内での出現/確信度） ---
                    entity_expand_debug["selected_entities"] = [
                        {
                            "type": str(t_norm),
                            "name_norm": str(name_norm),
                            "seed_cnt": int((stats.get((str(t_norm), str(name_norm))) or {}).get("cnt") or 0),
                            "seed_conf": float((stats.get((str(t_norm), str(name_norm))) or {}).get("conf") or 0.0),
                        }
                        for (t_norm, name_norm) in selected_entities
                    ]

                    # --- entity_expand で追加したキー（重複除外前のユニーク集合） ---
                    entity_expand_added_keys: set[tuple[str, int]] = set()

                    # --- entityごとに関連イベント/状態を追加 ---
                    for (t_norm, name_norm) in selected_entities:
                        # events
                        if int(per_entity_event_limit) > 0:
                            rows = (
                                db.query(EventEntity.event_id)
                                .join(Event, Event.event_id == EventEntity.event_id)
                                .filter(Event.searchable == 1)
                                .filter(EventEntity.entity_type_norm == str(t_norm))
                                .filter(EventEntity.entity_name_norm == str(name_norm))
                                .order_by(Event.event_id.desc())
                                .limit(int(per_entity_event_limit))
                                .all()
                            )
                            keys = [("event", int(r[0])) for r in rows if r and r[0] is not None]
                            for t_i, id_i in keys:
                                entity_expand_added_keys.add((str(t_i), int(id_i)))
                            add_sources(keys, label="entity_expand")

                        # states
                        if int(per_entity_state_limit) > 0:
                            rows = (
                                db.query(StateEntity.state_id)
                                .join(State, State.state_id == StateEntity.state_id)
                                .filter(State.searchable == 1)
                                .filter(StateEntity.entity_type_norm == str(t_norm))
                                .filter(StateEntity.entity_name_norm == str(name_norm))
                                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                                .limit(int(per_entity_state_limit))
                                .all()
                            )
                            keys = [("state", int(r[0])) for r in rows if r and r[0] is not None]
                            for t_i, id_i in keys:
                                entity_expand_added_keys.add((str(t_i), int(id_i)))
                            add_sources(keys, label="entity_expand")

                    # --- デバッグ用: 追加キー数（種別別） ---
                    entity_expand_debug["added_keys"] = {
                        "event": sum(1 for (t, _i) in entity_expand_added_keys if str(t) == "event"),
                        "state": sum(1 for (t, _i) in entity_expand_added_keys if str(t) == "state"),
                    }

        collect_debug["entity_expand"] = entity_expand_debug

        # --- レコードをまとめて引く（ORMのDetachedを避けるため、候補のdict化までセッション内で行う） ---
        keys_all = sorted([k for k in sources_by_key.keys() if not (k[0] == "event" and int(k[1]) == int(event_id))])
        if not keys_all:
            # --- 候補ゼロでも原因が追えるように、デバッグを残す ---
            self._log_retrieval_debug(  # type: ignore[attr-defined]
                "（（候補収集デバッグ））",
                collect_debug,
            )
            return [], collect_debug

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
                                "vad": {"v": float(a.vad_v), "a": float(a.vad_a), "d": float(a.vad_d)},
                                "confidence": float(a.confidence),
                            },
                            hit_sources=hit_sources,
                        )
                    )

        # --- デバッグ: 埋め込みDB（vec_items）由来の候補を表示する ---
        # NOTE:
        # - ここはLLMへ投げる情報の一部なので、LLM I/O ログ（DEBUG）へ揃えて出す。
        # - vec由来候補（recent/global）を中心に表示する（取りこぼしと重複の確認用）。
        vector_loaded_preview = [c.meta for c in out if {"vector_recent", "vector_global"} & set(c.hit_sources or [])][:30]
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

        # --- quota（経路偏り抑制）: modeごとの割合配分で候補を間引く ---
        # NOTE:
        # - ここでの間引きは「選別入力の安定化」が目的。取りこぼしを減らすため、余りは元の順位順で埋める。
        out = self._apply_source_quota(candidates=out, plan_obj=plan_obj, max_candidates=int(max_candidates))

        # --- 収集デバッグをログへ出す ---
        # NOTE: LLMログ（DEBUG）が有効な場合のみ出る（通常運用では負荷になりにくい）。
        self._log_retrieval_debug(  # type: ignore[attr-defined]
            "（（候補収集デバッグ））",
            collect_debug,
        )

        return out[:max_candidates], collect_debug

    def _apply_diversify_inplace(self, *, candidates: list[_CandidateItem], plan_obj: dict[str, Any]) -> list[_CandidateItem]:
        """
        plan_obj.diversify に基づき、event候補の並び順だけを調整する。

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
