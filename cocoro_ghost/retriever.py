"""Contextual Memory Retrieval（文脈考慮型の記憶検索）。"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from cocoro_ghost.db import EPISODE_FTS_TABLE_NAME, search_similar_unit_ids
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.unit_enums import Sensitivity, UnitKind
from cocoro_ghost.unit_models import PayloadEpisode, Unit


Message = Mapping[str, str]

_FTS5_SPLIT_RE = re.compile(r"\s+")


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
    rrf_score: float


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
) -> tuple[list[int], dict[int, float]]:
    scores: dict[int, float] = defaultdict(float)

    for result_list in vector_results:
        for rank, unit_id in enumerate(result_list):
            scores[int(unit_id)] += 1.0 / (rrf_k + rank + 1)

    for result_list in bm25_results:
        for rank, unit_id in enumerate(result_list):
            scores[int(unit_id)] += 1.0 / (rrf_k + rank + 1)

    sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)[: max(0, max_candidates)]
    return [unit_id for unit_id, _score in sorted_items], {unit_id: float(score) for unit_id, score in sorted_items}


def _clip_text(text_: str, *, max_chars: int, tail: bool) -> str:
    t = _compact_text(text_)
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    return t[-max_chars:] if tail else t[:max_chars]


def _char_ngrams(text_: str, *, n: int, max_chars: int, tail: bool) -> set[str]:
    t = _clip_text(text_, max_chars=max_chars, tail=tail)
    if not t:
        return set()
    if len(t) <= n:
        return {t}
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter <= 0:
        return 0.0
    return (2.0 * float(inter)) / float(len(a) + len(b))


def _recency_score(*, now_ts: int, occurred_at: int, tau_days: float) -> float:
    if occurred_at <= 0:
        return 0.0
    age_days = max(0.0, float(now_ts - occurred_at) / 86400.0)
    tau = max(1.0, float(tau_days))
    return float(math.exp(-age_days / tau))


class Retriever:
    """Hybrid Search（Vector + BM25）→ Heuristic Rerank の2段階検索。"""

    _RECENT_CONVERSATION_TURNS = 3
    _KNN_K_PER_QUERY = 20
    _BM25_K_PER_QUERY = 20
    _MAX_SENSITIVITY = int(Sensitivity.PRIVATE)
    _OCCURRED_DAY_RANGE = 365
    _RRF_K = 60

    _RERANK_NGRAM_N = 3
    _RERANK_QUERY_MAX_CHARS = 1200
    _RERANK_EPISODE_MAX_CHARS = 1200
    _RERANK_RECENCY_TAU_DAYS = 45.0
    _RERANK_DUP_THRESHOLD = 0.90
    _RERANK_HIGH_THRESHOLD = 0.35
    _RERANK_MEDIUM_THRESHOLD = 0.28
    _RERANK_W_RRF = 0.55
    _RERANK_W_LEX = 0.35
    _RERANK_W_REC = 0.10

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
        max_candidates: int = 60,
        max_results: int = 5,
    ) -> list[RankedEpisode]:
        candidates = self._search_candidates(user_text, recent_conversation, max_candidates)
        ranked = self._rerank(user_text, recent_conversation, candidates, max_results)
        return ranked

    def _search_candidates(
        self,
        user_text: str,
        recent_conversation: Sequence[Message],
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

        # Fixed multi-query (no LLM query expansion):
        # - user_text only (avoid recent context dominance)
        # - context + user_text (follow conversational continuity)
        all_queries: list[str] = []
        user_only_query = user_text
        if user_only_query:
            all_queries.append(user_only_query)
        if original_query and original_query not in all_queries:
            all_queries.append(original_query)

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

        candidate_ids, rrf_scores = _rrf_merge(
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
                rrf_score=float(rrf_scores.get(int(u.id), 0.0)),
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

        now_ts = int(time.time())
        query_text = f"{context}\n---\n{user_text}" if context else user_text
        query_ngrams = _char_ngrams(
            query_text,
            n=self._RERANK_NGRAM_N,
            max_chars=self._RERANK_QUERY_MAX_CHARS,
            tail=True,
        )
        strength = min(1.0, float(len(query_ngrams)) / 30.0) if query_ngrams else 0.0

        max_rrf = max((float(c.rrf_score) for c in candidates), default=0.0)
        if max_rrf <= 0.0:
            max_rrf = 1.0

        scored: list[tuple[float, float, float, float, CandidateEpisode, set[str]]] = []
        for c in candidates:
            episode_text = "\n".join([c.user_text, c.reply_text]).strip()
            episode_ngrams = _char_ngrams(
                episode_text,
                n=self._RERANK_NGRAM_N,
                max_chars=self._RERANK_EPISODE_MAX_CHARS,
                tail=False,
            )
            lex = _dice(query_ngrams, episode_ngrams) * strength
            rec = _recency_score(now_ts=now_ts, occurred_at=int(c.occurred_at), tau_days=self._RERANK_RECENCY_TAU_DAYS)
            rrf_norm = float(c.rrf_score) / float(max_rrf)
            final = (
                self._RERANK_W_RRF * rrf_norm
                + self._RERANK_W_LEX * lex
                + self._RERANK_W_REC * rec
            )
            scored.append((final, rrf_norm, lex, rec, c, episode_ngrams))

        scored.sort(key=lambda x: x[0], reverse=True)

        picked: list[tuple[float, float, float, float, CandidateEpisode]] = []
        picked_ngrams: list[set[str]] = []
        for final, rrf_norm, lex, rec, c, episode_ngrams in scored:
            if len(picked) >= max_results:
                break
            if final < self._RERANK_MEDIUM_THRESHOLD:
                continue
            if picked_ngrams and episode_ngrams:
                if any(_dice(episode_ngrams, prev) >= self._RERANK_DUP_THRESHOLD for prev in picked_ngrams):
                    continue
            picked.append((final, rrf_norm, lex, rec, c))
            picked_ngrams.append(episode_ngrams)

        if not picked or picked[0][0] < self._RERANK_HIGH_THRESHOLD:
            return []

        ranked: list[RankedEpisode] = []
        for idx, (final, rrf_norm, lex, rec, c) in enumerate(picked):
            relevance: Literal["high", "medium"] = "high" if idx == 0 else "medium"
            reason = f"heuristic rerank: score={final:.3f} rrf={rrf_norm:.3f} lex={lex:.3f} rec={rec:.3f}"
            ranked.append(
                RankedEpisode(
                    unit_id=int(c.unit_id),
                    user_text=c.user_text,
                    reply_text=c.reply_text,
                    occurred_at=int(c.occurred_at),
                    relevance=relevance,
                    reason=reason,
                )
            )

        return ranked
