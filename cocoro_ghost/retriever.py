"""Contextual Memory Retrieval（文脈考慮型の記憶検索）。"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Literal, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from cocoro_ghost import prompts
from cocoro_ghost.db import EPISODE_FTS_TABLE_NAME, search_similar_unit_ids
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.unit_enums import Sensitivity, UnitKind
from cocoro_ghost.unit_models import PayloadEpisode, Unit


Message = Mapping[str, str]

_FTS5_SPLIT_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ExpandedQuery:
    query: str
    reference_type: str | None  # anaphora, temporal, ellipsis, topic
    original_surface: str | None


@dataclass(frozen=True)
class RankedEpisode:
    unit_id: int
    user_text: str
    reply_text: str
    occurred_at: int
    relevance: Literal["high", "medium"]
    reason: str


@dataclass(frozen=True)
class CandidateEpisode:
    unit_id: int
    user_text: str
    reply_text: str
    occurred_at: int


def _compact_text(text_: str) -> str:
    return " ".join((text_ or "").replace("\r", "").replace("\n", " ").split()).strip()


def _escape_fts5_query(raw: str, *, max_terms: int = 12) -> str:
    """
    FTS5 MATCH クエリの構文エラーを避けるため、ユーザー入力を安全なクエリに変換する。

    - 句読点/記号を含む場合もパースできるよう、各termを "..." で囲む
    - 複数termは OR でつなぎ、過剰に絞り込まない
    """
    text_ = _compact_text(raw)
    if not text_:
        return '""'

    terms = [t for t in _FTS5_SPLIT_RE.split(text_) if t]
    if not terms:
        return '""'

    escaped_terms: list[str] = []
    for term in terms[: max(1, int(max_terms))]:
        t = term.replace('"', '""')
        escaped_terms.append(f'"{t}"')

    if not escaped_terms:
        return '""'
    if len(escaped_terms) == 1:
        return escaped_terms[0]
    return " OR ".join(escaped_terms)


def _format_recent_conversation(recent_conversation: Sequence[Message], *, max_messages: int) -> str:
    if not recent_conversation:
        return ""
    messages = list(recent_conversation)[-max_messages:]
    lines: list[str] = []
    for m in messages:
        role = _compact_text(str(m.get("role") or ""))
        content = _compact_text(str(m.get("content") or ""))
        if not content:
            continue
        if role == "user":
            label = "User"
        elif role in {"assistant", "partner"}:
            label = "Partner"
        else:
            label = role or "Message"
        lines.append(f"{label}: {content}")
    return "\n".join(lines).strip()


def _rrf_merge(
    vector_results: Sequence[Sequence[int]],
    bm25_results: Sequence[Sequence[int]],
    *,
    max_candidates: int,
    rrf_k: int,
) -> list[int]:
    scores: dict[int, float] = defaultdict(float)

    for result_list in vector_results:
        for rank, unit_id in enumerate(result_list):
            scores[int(unit_id)] += 1.0 / (rrf_k + rank + 1)

    for result_list in bm25_results:
        for rank, unit_id in enumerate(result_list):
            scores[int(unit_id)] += 1.0 / (rrf_k + rank + 1)

    sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [unit_id for unit_id, _score in sorted_items[: max(0, max_candidates)]]


class Retriever:
    """Query Expansion → Hybrid Search（Vector + BM25）→ LLM Reranking の3段階検索。"""

    _MAX_EXPANDED_QUERIES = 5
    _RECENT_CONVERSATION_TURNS = 3
    _KNN_K_PER_QUERY = 10
    _BM25_K_PER_QUERY = 10
    _MAX_SENSITIVITY = int(Sensitivity.PRIVATE)
    _OCCURRED_DAY_RANGE = 365
    _RRF_K = 60

    def __init__(self, llm_client: LlmClient, db: Session):
        self.llm_client = llm_client
        self.db = db
        self.logger = logging.getLogger(__name__)
        self._last_injection_strategy: str = "quote_key_parts"

    @property
    def last_injection_strategy(self) -> str:
        return self._last_injection_strategy

    def retrieve(
        self,
        user_text: str,
        recent_conversation: Sequence[Message],
        *,
        max_candidates: int = 30,
        max_results: int = 5,
    ) -> list[RankedEpisode]:
        expanded = self._expand_queries(user_text, recent_conversation)
        candidates = self._search_candidates(user_text, recent_conversation, expanded, max_candidates)
        ranked = self._rerank(user_text, recent_conversation, candidates, max_results)
        return ranked

    def _expand_queries(self, user_text: str, recent_conversation: Sequence[Message]) -> list[ExpandedQuery]:
        user_text = (user_text or "").strip()
        if not user_text:
            return []

        context = _format_recent_conversation(
            recent_conversation,
            max_messages=self._RECENT_CONVERSATION_TURNS * 2,
        )
        payload = "\n".join(
            [
                f"ユーザー発話: {user_text}",
                "直近の会話:",
                context or "(なし)",
            ]
        ).strip()

        try:
            resp = self.llm_client.generate_json_response(
                system_prompt=prompts.get_retrieval_query_expansion_prompt(),
                user_text=payload,
                temperature=0.2,
                max_tokens=512,
            )
            data = json.loads(self.llm_client.response_content(resp))
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("query expansion failed", exc_info=exc)
            return []

        expanded_raw = data.get("expanded_queries") if isinstance(data, dict) else None
        refs_raw = data.get("detected_references") if isinstance(data, dict) else None

        expanded: list[ExpandedQuery] = []
        seen: set[str] = set()

        def add_query(q: str, reference_type: str | None, original_surface: str | None) -> None:
            if len(expanded) >= self._MAX_EXPANDED_QUERIES:
                return
            q = (q or "").strip()
            if not q or q == user_text:
                return
            key = q
            if key in seen:
                return
            seen.add(key)
            expanded.append(
                ExpandedQuery(
                    query=q,
                    reference_type=(reference_type or "").strip() or None,
                    original_surface=(original_surface or "").strip() or None,
                )
            )

        if isinstance(expanded_raw, list):
            for q in expanded_raw:
                add_query(str(q), None, None)

        if isinstance(refs_raw, list):
            for r in refs_raw:
                if not isinstance(r, dict):
                    continue
                resolved = str(r.get("resolved") or "").strip()
                if not resolved:
                    continue
                add_query(resolved, str(r.get("type") or "").strip() or None, str(r.get("surface") or "").strip() or None)

        return expanded

    def _search_candidates(
        self,
        user_text: str,
        recent_conversation: Sequence[Message],
        expanded: Sequence[ExpandedQuery],
        max_candidates: int,
    ) -> list[CandidateEpisode]:
        user_text = (user_text or "").strip()
        if not user_text or max_candidates <= 0:
            return []

        context = _format_recent_conversation(
            recent_conversation,
            max_messages=self._RECENT_CONVERSATION_TURNS * 2,
        )
        if context:
            original_query = f"{context}\n---\n{user_text}"
        else:
            original_query = user_text

        all_queries: list[str] = [original_query]
        for e in expanded:
            q = (e.query or "").strip()
            if not q:
                continue
            if q not in all_queries:
                all_queries.append(q)

        all_queries = all_queries[: 1 + self._MAX_EXPANDED_QUERIES]

        try:
            embeddings = self.llm_client.generate_embedding(all_queries)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("embedding failed", exc_info=exc)
            return []

        now_ts = int(time.time())
        d1 = now_ts // 86400
        d0 = d1 - self._OCCURRED_DAY_RANGE

        vector_results: list[list[int]] = []
        for emb in embeddings:
            try:
                rows = search_similar_unit_ids(
                    self.db,
                    query_embedding=emb,
                    k=self._KNN_K_PER_QUERY,
                    kind=int(UnitKind.EPISODE),
                    max_sensitivity=self._MAX_SENSITIVITY,
                    occurred_day_range=(d0, d1),
                )
                vector_results.append([int(r.unit_id) for r in rows])
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("vector search failed", exc_info=exc)
                vector_results.append([])

        bm25_results: list[list[int]] = []
        for q in all_queries:
            bm25_results.append(self._bm25_search(q, k=self._BM25_K_PER_QUERY, occurred_day_range=(d0, d1)))

        candidate_ids = _rrf_merge(
            vector_results,
            bm25_results,
            max_candidates=max_candidates,
            rrf_k=self._RRF_K,
        )
        if not candidate_ids:
            return []

        rows = (
            self.db.query(Unit, PayloadEpisode)
            .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
            .filter(
                Unit.id.in_(candidate_ids),
                Unit.kind == int(UnitKind.EPISODE),
                Unit.state.in_([0, 1, 2]),
                Unit.sensitivity <= self._MAX_SENSITIVITY,
            )
            .all()
        )
        by_id: dict[int, CandidateEpisode] = {}
        for u, pe in rows:
            ts = int(u.occurred_at or u.created_at or now_ts)
            by_id[int(u.id)] = CandidateEpisode(
                unit_id=int(u.id),
                user_text=(pe.user_text or "").strip(),
                reply_text=(pe.reply_text or "").strip(),
                occurred_at=ts,
            )

        ordered: list[CandidateEpisode] = []
        for uid in candidate_ids:
            c = by_id.get(int(uid))
            if c is not None:
                ordered.append(c)
        return ordered

    def _bm25_search(self, query: str, *, k: int, occurred_day_range: tuple[int, int]) -> list[int]:
        raw = _compact_text(str(query or ""))
        if not raw or k <= 0:
            return []

        # FTS5の構文と衝突しやすい文字を含むケースがあるため、MATCH用にエスケープする。
        q = _escape_fts5_query(raw[:256])

        day_filter = ""
        params: dict[str, Any] = {
            "query": q,
            "k": int(k),
            "kind": int(UnitKind.EPISODE),
            "max_sensitivity": int(self._MAX_SENSITIVITY),
            "d0": int(occurred_day_range[0]),
            "d1": int(occurred_day_range[1]),
        }
        day_filter = "AND CAST(COALESCE(u.occurred_at, u.created_at) / 86400 AS INT) BETWEEN :d0 AND :d1"

        table = EPISODE_FTS_TABLE_NAME
        sql = f"""
        SELECT {table}.rowid AS unit_id, bm25({table}) AS score
        FROM {table}
        JOIN units u ON u.id = {table}.rowid
        WHERE {table} MATCH :query
          AND u.kind = :kind
          AND u.state IN (0, 1, 2)
          AND u.sensitivity <= :max_sensitivity
          {day_filter}
        ORDER BY score
        LIMIT :k
        """

        try:
            rows = self.db.execute(text(sql), params).fetchall()
            return [int(r[0]) for r in rows]
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("bm25 search failed", exc_info=exc)
            return []

    def _rerank(
        self,
        user_text: str,
        recent_conversation: Sequence[Message],
        candidates: Sequence[CandidateEpisode],
        max_results: int,
    ) -> list[RankedEpisode]:
        self._last_injection_strategy = "quote_key_parts"

        user_text = (user_text or "").strip()
        if not user_text or not candidates or max_results <= 0:
            return []

        context = _format_recent_conversation(
            recent_conversation,
            max_messages=self._RECENT_CONVERSATION_TURNS * 2,
        )

        def _truncate(text_: str, limit: int) -> str:
            t = _compact_text(text_)
            if limit <= 0 or len(t) <= limit:
                return t
            return t[:limit].rstrip() + "…"

        parts: list[str] = []
        for c in candidates:
            dt = datetime.fromtimestamp(int(c.occurred_at), tz=timezone.utc).strftime("%Y-%m-%d")
            ut = _truncate(c.user_text, 220)
            rt = _truncate(c.reply_text, 260)
            block = "\n".join(
                [
                    f"[unit_id={c.unit_id}] date={dt}",
                    f"User: {ut}" if ut else "User: (empty)",
                    f"Partner: {rt}" if rt else "Partner: (empty)",
                ]
            )
            parts.append(block)
        candidates_formatted = "\n\n".join(parts)

        payload = "\n\n".join(
            [
                f"現在のユーザー発話:\n{user_text}",
                "直近の会話文脈:",
                context or "(なし)",
                "候補エピソード:",
                candidates_formatted,
            ]
        ).strip()

        try:
            resp = self.llm_client.generate_json_response(
                system_prompt=prompts.get_retrieval_rerank_prompt(),
                user_text=payload,
                temperature=0.0,
                max_tokens=768,
            )
            data = json.loads(self.llm_client.response_content(resp))
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("rerank failed", exc_info=exc)
            return []

        if not isinstance(data, dict):
            return []

        strategy = str(data.get("injection_strategy") or "").strip()
        if strategy in {"quote_key_parts", "summarize", "full"}:
            self._last_injection_strategy = strategy

        raw_items = data.get("relevant_episodes") or []
        if not isinstance(raw_items, list):
            return []

        cand_by_id: dict[int, CandidateEpisode] = {int(c.unit_id): c for c in candidates}
        ranked_raw: list[RankedEpisode] = []
        seen: set[int] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                unit_id = int(item.get("unit_id"))
            except Exception:  # noqa: BLE001
                continue
            if unit_id in seen:
                continue
            cand = cand_by_id.get(unit_id)
            if cand is None:
                continue

            relevance = str(item.get("relevance") or "").strip()
            if relevance not in {"high", "medium"}:
                continue
            reason = _truncate(str(item.get("reason") or ""), 240)

            ranked_raw.append(
                RankedEpisode(
                    unit_id=unit_id,
                    user_text=cand.user_text,
                    reply_text=cand.reply_text,
                    occurred_at=cand.occurred_at,
                    relevance=relevance,  # type: ignore[arg-type]
                    reason=reason,
                )
            )
            seen.add(unit_id)
            if len(ranked_raw) >= max_results:
                break

        highs = [e for e in ranked_raw if e.relevance == "high"]
        mediums = [e for e in ranked_raw if e.relevance == "medium"]
        ranked = highs + mediums
        return ranked[:max_results]
