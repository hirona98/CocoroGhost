"""
チャット（/api/chat）用の記憶処理（検索・選別・内部コンテキスト・SSE）。

目的:
    - `MemoryManager` を分割するため、チャット経路の実装を mixin に切り出す。
    - 出来事ログ（events）作成 → 検索 → 返答ストリーム → 非同期更新のキックまでを担う。
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import time
from typing import Any, Generator

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import text

from cocoro_ghost import schemas
from cocoro_ghost import affect
from cocoro_ghost import common_utils, prompt_builders, vector_index
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_debug import log_llm_payload, normalize_llm_log_level
from cocoro_ghost.llm_client import LlmRequestPurpose
from cocoro_ghost.memory._chat_search_mixin import _CandidateItem
from cocoro_ghost.memory._image_mixin import default_input_text_when_images_only
from cocoro_ghost.memory_models import Event, EventAssistantSummary, EventLink, RetrievalRun, State
from cocoro_ghost.memory._utils import now_utc_ts
from cocoro_ghost.time_utils import format_iso8601_local


logger = logging.getLogger(__name__)
_warned_memory_disabled = False

# NOTE:
# - 検索ヘルパー（FTSクエリ生成/画像要約パース）は `_chat_search_mixin.py` に集約した。
# - `stream_chat` は `self._collect_candidates()` を呼び、検索自体は `_ChatSearchMixin` が担当する。
def _sse(event: str, data: dict) -> str:
    """SSEの1イベントを文字列化する。"""
    return f"event: {event}\n" + f"data: {common_utils.json_dumps(data)}\n\n"


# --- SearchResultPack 選別: 入力を圧縮して体感速度を上げる ---
#
# 背景:
# - 選別（SearchResultPack）はSSE開始前の同期経路なので、ここが遅いと体感が悪化する。
# - 候補の「本文」をそのまま渡すと入力トークンが増え、選別が遅く/不安定になりやすい。
# - 一方で、出力（selected）は ID を返せば、後段でDBから詳細を注入できる。
#
# 方針:
# - 候補は「短いプレビュー＋メタ情報」に圧縮し、キー名も短縮する。
# - 出力スキーマ（selectedの type/event_id/state_id/affect_id 等）は維持する。
_HIT_SOURCE_TO_CODE: dict[str, str] = {
    "recent_events": "re",
    "trigram_events": "tg",
    "reply_chain": "rc",
    "context_threads": "ct",
    "context_links": "cl",
    "recent_states": "rs",
    "about_time": "at",
    "vector_recent": "vr",
    "vector_global": "vg",
}


# --- RetrievalPlan（SearchPlan）: ルールベース ---
#
# 背景:
# - SearchPlan（LLM）を毎ターン呼ぶと、SSE開始前の同期待ちが増えて体感が悪化する。
# - 一方で「昔話/期間指定」だけは拾い漏れやすいので、ユーザーが明示した時だけ軽いルールで補助する。
#
# 方針:
# - LLMによるSearchPlan生成は行わない（常にルールで固定planを作る）。
# - mode は「ユーザーが明示した期間ヒントがあるか」でのみ切り替える（指定を要求しない）。
_YEAR_RE = re.compile(r"(?:^|[^0-9])((?:19|20)[0-9]{2})(?:[^0-9]|$)")


def _preview_text_for_selection(text_in: str, *, limit_chars: int) -> str:
    """LLM選別入力向けにテキストを短く整形する。

    目的:
        - 入力トークンを削減して選別を高速化する。
        - 重要情報が末尾にある場合もあるため、単純な先頭切り捨てではなく「頭+末尾」を残す。

    Args:
        text_in: 入力テキスト。
        limit_chars: 目安の最大文字数（文字数ベースで切る）。
    """

    # --- 正規化（会話装飾タグ除去＋空白整形） ---
    s = common_utils.strip_face_tags(str(text_in or ""))
    if not s:
        return ""

    # --- 既に短いならそのまま ---
    limit = max(1, int(limit_chars))
    if len(s) <= limit:
        return s

    # --- 頭+末尾で保持（末尾側の手がかりを残す） ---
    head_len = max(1, int(limit * 0.65))
    tail_len = max(1, int(limit - head_len - 1))
    head = s[:head_len].rstrip()
    tail = s[-tail_len:].lstrip() if tail_len > 0 else ""
    if not tail:
        return head
    return f"{head}…{tail}"


def _encode_hit_sources_for_selection(hit_sources: list[str]) -> list[str]:
    """hit_sources を短いコード列に圧縮する。

    NOTE:
        - 未知のラベルは落とさずにそのまま残す（診断性と品質を優先）。
    """

    # --- 正規化 ---
    hs = [str(x or "").strip() for x in (hit_sources or [])]
    hs = [x for x in hs if x]
    if not hs:
        return []

    # --- 短縮コード化 ---
    out: list[str] = []
    for x in hs:
        out.append(_HIT_SOURCE_TO_CODE.get(x, x))

    # --- 重複除去（順序は安定化） ---
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


def _load_event_assistant_summaries_for_selection(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    candidates: list[_CandidateItem],
) -> dict[int, str]:
    """選別入力向けに、イベント要約（assistant_summary）をまとめてロードする。

    方針:
        - 返答生成（SearchResultPack注入）には使わず、あくまで「選別の材料」だけを軽くする。
        - events.updated_at と一致する要約のみ採用し、古い要約は無視する（安全寄り）。
    """

    # --- event_id を集める ---
    event_ids: list[int] = []
    for c in (candidates or []):
        if str(getattr(c, "type", "")) != "event":
            continue
        meta = getattr(c, "meta", None)
        if not isinstance(meta, dict):
            continue
        eid = int(meta.get("event_id") or 0)
        if eid > 0:
            event_ids.append(int(eid))
    event_ids = sorted({int(x) for x in event_ids if int(x) > 0})
    if not event_ids:
        return {}

    # --- DBからまとめて引く ---
    out: dict[int, str] = {}
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        rows = (
            db.query(EventAssistantSummary.event_id, EventAssistantSummary.summary_text)
            .join(Event, Event.event_id == EventAssistantSummary.event_id)
            .filter(EventAssistantSummary.event_id.in_([int(x) for x in event_ids]))
            .filter(EventAssistantSummary.event_updated_at == Event.updated_at)
            .all()
        )
        for r in rows:
            if not r:
                continue
            try:
                eid = int(r[0] or 0)
            except Exception:  # noqa: BLE001
                continue
            t = str(r[1] or "").strip()
            if eid <= 0 or not t:
                continue
            # --- 異常に長い要約は安全に切る（選別入力の肥大化を防ぐ） ---
            if len(t) > 600:
                t = t[:600]
            out[int(eid)] = t
    return out


def _build_rule_based_retrieval_plan(
    *,
    user_input: str,
    max_candidates: int,
) -> dict[str, Any]:
    """
    ルールベースでRetrievalPlan（SearchPlan互換のdict）を作る。

    目的:
        - SearchPlan（LLM）を廃止し、SSE開始前の同期待ちを減らす。
        - ただしユーザーが明示した「年/学生区分」などは拾えるようにして、期間指定の取りこぼしを抑える。

    Args:
        user_input: ユーザー入力（augmentedではなく、元の入力を推奨）。
        max_candidates: 候補収集の最大件数（上限は別途TOMLで強制される）。
    """

    # --- 正規化 ---
    text_in = str(user_input or "").strip()

    # --- time_hint: 年（4桁）を抽出 ---
    y0: int | None = None
    y1: int | None = None
    m = _YEAR_RE.search(text_in)
    if m:
        try:
            year = int(m.group(1))
            if 1900 <= int(year) <= 2100:
                y0 = int(year)
                y1 = int(year)
        except Exception:  # noqa: BLE001
            pass

    # --- time_hint: life_stage_hint を抽出（ユーザーが明示した場合のみ） ---
    # NOTE:
    # - ここは「期間指定を要求しない」が方針なので、強いキーワードがある場合だけヒントを入れる。
    # - 迷ったら空にして、探索枠（全期間vector）で“ひらめき”を狙う。
    life_stage_hint = ""
    if "小学生" in text_in:
        life_stage_hint = "elementary"
    elif "中学生" in text_in or "中学" in text_in:
        life_stage_hint = "middle"
    elif "高校" in text_in or "高1" in text_in or "高2" in text_in or "高3" in text_in:
        life_stage_hint = "high"
    elif "大学" in text_in:
        life_stage_hint = "university"
    elif "社会人" in text_in or "仕事" in text_in:
        life_stage_hint = "work"

    # --- mode: 明示ヒントがある場合だけ explicit_about_time とする ---
    # NOTE:
    # - これにより about_time 経路が走る。
    # - それ以外は associative_recent とし、会話の流れ（直近性）は別経路で担保する。
    mode = "associative_recent"
    if y0 is not None or y1 is not None or life_stage_hint:
        mode = "explicit_about_time"

    # --- plan を構築（SearchPlan互換） ---
    # NOTE:
    # - queries は「ユーザー入力そのまま」だけにする（追加生成は行わない）。
    # - limits.max_candidates は上位でTOMLにより強制されるが、入力/ログ整合性のためここにも反映する。
    plan_obj: dict[str, Any] = {
        "mode": str(mode),
        "queries": ([str(text_in)] if str(text_in) else []),
        "time_hint": {"about_year_start": y0, "about_year_end": y1, "life_stage_hint": str(life_stage_hint)},
        "diversify": {"by": ["life_stage", "about_year_bucket"], "per_bucket": 5},
        "limits": {"max_candidates": int(max(1, min(400, int(max_candidates)))), "max_selected": 12},
    }
    return plan_obj


def _build_compact_candidates_for_selection(
    candidates: list[_CandidateItem],
    *,
    event_id_to_summary: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """SearchResultPack選別用の候補リスト（圧縮形式）を作る。"""

    # --- プレビューは「品質を落とさず短く」を狙う ---
    # NOTE:
    # - ここはSSE開始前の同期経路なので、CPU側コストは最小にする（正規表現の多用は避ける）。
    # - 文字数上限は「候補数が多い」状況を想定し、過度に大きくしない。
    event_user_limit = 360
    event_asst_limit = 420
    state_body_limit = 420
    state_payload_limit = 180
    affect_moment_limit = 280

    # --- event_id -> assistant_summary_text（存在すればこちらを優先） ---
    # NOTE:
    # - 要約は worker が作る派生情報であり、返答生成（SearchResultPack注入）には使わない。
    # - 選別入力だけを軽くして、SSE開始までの待ちを削る。
    summary_map: dict[int, str] = dict(event_id_to_summary or {})

    out: list[dict[str, Any]] = []
    for c in (candidates or []):
        meta = dict(c.meta or {})
        hs = _encode_hit_sources_for_selection(list(c.hit_sources or []))

        # --- event ---
        if str(c.type) == "event":
            at = meta.get("about_time") if isinstance(meta.get("about_time"), dict) else {}
            event_id = int(meta.get("event_id") or 0)
            assistant_text_raw = str(meta.get("assistant_text") or "")
            assistant_summary = str(summary_map.get(int(event_id)) or "")
            assistant_for_selection = assistant_summary if assistant_summary else assistant_text_raw
            out.append(
                {
                    "t": "e",
                    "id": int(event_id),
                    "ts": str(meta.get("created_at") or ""),
                    "src": str(meta.get("source") or ""),
                    "th": list(meta.get("thread_keys") or []),
                    "u": _preview_text_for_selection(str(meta.get("user_text") or ""), limit_chars=event_user_limit),
                    "a": _preview_text_for_selection(
                        str(assistant_for_selection or ""), limit_chars=event_asst_limit
                    ),
                    "img": _preview_text_for_selection(
                        str(meta.get("image_summaries_preview") or ""), limit_chars=240
                    )
                    or None,
                    "at": {
                        "y0": at.get("about_year_start"),
                        "y1": at.get("about_year_end"),
                        "ls": str(at.get("life_stage") or ""),
                        "c": float(at.get("confidence") or 0.0),
                    },
                    "hs": hs,
                }
            )
            continue

        # --- state ---
        if str(c.type) == "state":
            out.append(
                {
                    "t": "s",
                    "id": int(meta.get("state_id") or 0),
                    "k": str(meta.get("kind") or ""),
                    "ts": str(meta.get("last_confirmed_at") or ""),
                    "b": _preview_text_for_selection(str(meta.get("body_text") or ""), limit_chars=state_body_limit),
                    "p": _preview_text_for_selection(
                        str(meta.get("payload_json") or ""), limit_chars=state_payload_limit
                    ),
                    "vf": meta.get("valid_from_ts"),
                    "vt": meta.get("valid_to_ts"),
                    "hs": hs,
                }
            )
            continue

        # --- event_affect ---
        if str(c.type) == "event_affect":
            vad = meta.get("vad") if isinstance(meta.get("vad"), dict) else {}
            out.append(
                {
                    "t": "a",
                    "id": int(meta.get("affect_id") or 0),
                    "eid": int(meta.get("event_id") or 0),
                    "ts": str(meta.get("created_at") or ""),
                    "ets": str(meta.get("event_created_at") or ""),
                    "m": _preview_text_for_selection(
                        str(meta.get("moment_affect_text") or ""), limit_chars=affect_moment_limit
                    ),
                    "lab": list(meta.get("moment_affect_labels") or []),
                    "vad": [float(vad.get("v") or 0.0), float(vad.get("a") or 0.0), float(vad.get("d") or 0.0)],
                    "c": float(meta.get("confidence") or 0.0),
                    "hs": hs,
                }
            )
            continue

    # --- 不正な候補は除外し、入力を安定させる ---
    # NOTE: LLMに無意味な0 IDを渡すと「存在しないID」を返すリスクが上がるため、ここで落とす。
    cleaned: list[dict[str, Any]] = []
    for x in out:
        t = str(x.get("t") or "")
        i = int(x.get("id") or 0)
        if t in ("e", "s", "a") and i > 0:
            cleaned.append(x)
    return cleaned


class _UserVisibleReplySanitizer:
    """ユーザーに見せる本文から、内部コンテキストの混入を除去する。"""

    # NOTE: 改行が出ないモデルでもストリーミング体感を維持するための最小送信単位。
    _STREAM_FLUSH_THRESHOLD_CHARS = 64

    def __init__(self) -> None:
        # --- feed()で改行が来るまで保留する末尾（行未確定） ---
        self._pending: str = ""

        # --- 内部ブロックをスキップ中かどうか ---
        self._skip_mode: bool = False

        # --- スキップ開始後に、空行以外を1行でも捨てたか ---
        self._skipped_any_line_in_block: bool = False

    def feed(self, text: str) -> str:
        """差分テキストを取り込み、ユーザーに送ってよいテキストだけ返す。"""

        # --- 空は即返す ---
        if not text:
            return ""

        # --- バッファへ追加 ---
        self._pending += text

        # --- 改行単位で確定処理 ---
        out_parts: list[str] = []
        while True:
            head, sep, tail = self._pending.partition("\n")
            if not sep:
                break
            line = head + sep
            self._pending = tail
            kept = self._process_line(line)
            if kept:
                out_parts.append(kept)

        # --- 改行が来ないモデルでも、体感速度（ストリーミング）を維持する ---
        # 方針:
        # - 内部用タグ（"<" 始まり）の混入を避けるため、"<" を含む場合は保留する。
        # - それ以外は一定文字数で分割して送る（行単位以外でも流れるようにする）。
        if not self._skip_mode and self._pending and "<" not in self._pending:
            # NOTE: 小さすぎると無駄なイベントが増えるため、ほどよい粒度で送る。
            if len(self._pending) >= int(self._STREAM_FLUSH_THRESHOLD_CHARS):
                out_parts.append(self._pending)
                self._pending = ""
        return "".join(out_parts)

    def flush(self) -> str:
        """末尾（改行が無い行）を確定し、送ってよいテキストだけ返す。"""
        if not self._pending:
            return ""
        tail = self._process_line(self._pending)
        self._pending = ""
        return tail

    def _process_line(self, line: str) -> str:
        """1行分を処理し、送信する場合はそのまま返す。"""
        stripped_line = line.rstrip("\n").rstrip("\r").strip()

        # --- 内部ブロックの終端検出 ---
        if self._skip_mode:
            if stripped_line:
                self._skipped_any_line_in_block = True
                return ""
            if self._skipped_any_line_in_block:
                self._skip_mode = False
                self._skipped_any_line_in_block = False
            return ""

        # --- 内部っぽい行が来たら、その行から次の空行まで捨てる ---
        if self._is_internal_line(stripped_line):
            self._skip_mode = True
            self._skipped_any_line_in_block = False
            return ""

        return line

    def _is_internal_line(self, stripped_line: str) -> bool:
        """内部用の制御行/見出しに見えるかを判定する。"""
        if not stripped_line:
            return False

        # --- 内部コンテキスト開始タグ ---
        if stripped_line == "<<INTERNAL_CONTEXT>>":
            return True

        # --- 明示的な内部見出し ---
        if stripped_line.startswith("<<<") and stripped_line.endswith(">>>"):
            return True

        return False


class _ChatMemoryMixin:
    """チャット（/api/chat）経路の実装（mixin）。"""

    def _should_auto_forget_for_feedback_text(self, feedback_text: str) -> bool:
        """
        ユーザー入力が「関係ない/それじゃない」系の否定フィードバックに見えるかを判定する。

        方針:
        - 事故率（誤爆）を下げるため、強い否定の一部だけに反応する。
        - ここは軽量であることを優先し、文字列検索だけで判定する。
        """

        # --- 入力を正規化 ---
        t = str(feedback_text or "").strip()
        if not t:
            return False

        # --- 強い否定（最小集合） ---
        # NOTE:
        # - 「違う」単体は誤爆しやすいので入れない。
        # - 「関係ない」はユーザー要望（例: "それは関係ないよ"）に合わせてトリガに含める。
        triggers = [
            "関係ない",
            "それじゃない",
            "それは違う",
            "その話じゃない",
            "出さないで",
            "忘れて",
            "思い出さないで",
        ]
        return any(k in t for k in triggers)

    def _auto_forget_from_negative_feedback(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        feedback_event_id: int,
        reply_to_event_id: int | None,
        feedback_text: str,
    ) -> None:
        """
        否定フィードバック（例: 「それは関係ない」）を契機に、直前ターンで想起した記憶を自動で分離する。

        重要:
        - チャットの体感速度を壊さないため、この処理は BackgroundTasks で実行される想定。
        - 安全寄り（誤爆回避）のため、対象は「直前ターンの retrieval_runs.selected が1件」の場合に限定する。
        - UIや確認質問は行わない（会話のみ、かつ曖昧なら何もしない）。

        分離の定義:
        - event/state: 行は残しつつ `searchable=0` にして検索・候補収集・埋め込みから除外する。
        - event_affect: 行を削除する（内部用であり、再利用価値が低い前提）。
        """

        # --- トリガ判定（軽量） ---
        if not self._should_auto_forget_for_feedback_text(str(feedback_text or "")):
            return

        # --- reply_to が無いなら対象が無い ---
        if reply_to_event_id is None or int(reply_to_event_id) <= 0:
            return

        now_ts = now_utc_ts()

        # --- 直前ターンの retrieval_run を読む ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rr = (
                db.query(RetrievalRun)
                .filter(RetrievalRun.event_id == int(reply_to_event_id))
                .order_by(RetrievalRun.run_id.desc())
                .first()
            )
            if rr is None:
                return

            # --- selected_json をパース ---
            pack = common_utils.json_loads_maybe(str(rr.selected_json or "").strip())
            if not pack:
                return

            selected = pack.get("selected")
            if not isinstance(selected, list):
                return
            if len(selected) != 1:
                return

            item0 = selected[0]
            if not isinstance(item0, dict):
                return

            # --- type が無い場合はIDの有無で推定する（LLMの揺れ吸収） ---
            t = str(item0.get("type") or "").strip()
            if not t:
                if int(item0.get("event_id") or 0) > 0:
                    t = "event"
                elif int(item0.get("state_id") or 0) > 0:
                    t = "state"
                elif int(item0.get("affect_id") or 0) > 0:
                    t = "event_affect"
                else:
                    return

            # --- 対象IDを確定 ---
            target_event_id = int(item0.get("event_id") or 0)
            target_state_id = int(item0.get("state_id") or 0)
            target_affect_id = int(item0.get("affect_id") or 0)

            # --- 不整合は何もしない（安全寄り） ---
            if t == "event" and not (target_event_id > 0 and target_state_id == 0 and target_affect_id == 0):
                return
            if t == "state" and not (target_state_id > 0 and target_event_id == 0 and target_affect_id == 0):
                return
            if t == "event_affect" and not (target_affect_id > 0 and target_event_id == 0 and target_state_id == 0):
                return
            if t not in ("event", "state", "event_affect"):
                return

            # --- 分離（永続・不可逆）を適用 ---
            if t == "event":
                db.execute(
                    text("UPDATE events SET searchable=0, updated_at=:u WHERE event_id=:id"),
                    {"u": int(now_ts), "id": int(target_event_id)},
                )
                # --- 埋め込み復活を防ぐ（vec_items を消す） ---
                item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT), int(target_event_id))
                db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
                return

            if t == "state":
                db.execute(
                    text("UPDATE state SET searchable=0, updated_at=:u WHERE state_id=:id"),
                    {"u": int(now_ts), "id": int(target_state_id)},
                )
                # --- 埋め込み復活を防ぐ（vec_items を消す） ---
                item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_STATE), int(target_state_id))
                db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
                return

            # --- event_affect は行ごと削除する ---
            if t == "event_affect":
                db.execute(text("DELETE FROM event_affects WHERE id=:id"), {"id": int(target_affect_id)})
                item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT_AFFECT), int(target_affect_id))
                db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
                return

    def _load_recent_chat_dialog_messages(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        client_id: str,
        exclude_event_id: int,
        max_turn_events: int,
    ) -> list[dict[str, str]]:
        """
        直近のチャット会話（短期コンテキスト）を messages 形式で返す。

        注意:
            - クライアントは単純I/Oなので、サーバ側で直近会話を付与して会話の安定性を上げる。
            - ここは「会話の流れ」を補助する目的。検索（記憶）は別経路（SearchResultPack）。
            - with を抜けても安全なように、ORMを返さず dict だけ返す。
        """
        cid = str(client_id or "").strip()
        if not cid:
            return []

        # --- 直近ターン数（短期コンテキスト） ---
        # NOTE: max_turns_window は常に設定される前提（欠損フォールバックはしない）。
        n = int(max_turn_events)
        if n <= 0:
            return []
        n = max(1, n)

        # --- 1イベント=1ターン（user_text + assistant_text）を想定 ---
        rows: list[tuple[int, str, str]] = []
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            q = (
                db.query(Event.event_id, Event.user_text, Event.assistant_text)
                .filter(Event.source == "chat")
                .filter(Event.client_id == cid)
                .filter(Event.event_id != int(exclude_event_id))
                # assistant_text が無いターン（作成途中）は除外する
                .filter(Event.assistant_text.isnot(None))
                .order_by(Event.event_id.desc())
                .limit(int(n))
            )
            for r in q.all():
                if not r:
                    continue
                eid = int(r[0] or 0)
                ut = str(r[1] or "")
                at = str(r[2] or "")
                rows.append((eid, ut, at))

        # --- 新しい順で取っているので、会話としては古い順に並べ直す ---
        rows.reverse()

        out: list[dict[str, str]] = []
        # NOTE: メッセージは切り詰めず、そのまま送る。
        for _, ut, at in rows:
            if str(ut or "").strip():
                out.append({"role": "user", "content": str(ut)})
            if str(at or "").strip():
                out.append({"role": "assistant", "content": str(at)})
        return out

    def _load_long_mood_state_snapshot(
        self, *, embedding_preset_id: str, embedding_dimension: int, now_ts: int
    ) -> dict[str, Any] | None:
        """
        長期気分（state.kind="long_mood_state"）の最新スナップショットを返す。

        目的:
            - 返答生成で「背景の気分」を安定して参照できるようにする（SearchResultPackの選別に依存しない）。

        注意:
            - 1ユーザー前提（client_id は端末識別）のため、ここでは client_id で分けない。
            - with を抜けても安全なように、ORMを返さず dict だけ返す。
        """
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            st = (
                db.query(State)
                .filter(State.kind == "long_mood_state")
                .filter(State.searchable == 1)
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .first()
            )
            if st is None:
                return None

            # --- payload_json を dict として扱えるようにする ---
            payload_obj: Any = affect.parse_long_mood_payload(str(st.payload_json or ""))

            # --- VAD（baseline + shock）を計算する ---
            # NOTE:
            # - shock は「余韻」なので、読み出し時点（now_ts）で時間減衰させる。
            # - 更新（apply_write_plan）はイベント間隔で更新するが、無操作時間でも自然に落ち着く必要がある。
            baseline_vad: dict[str, float] | None = None
            shock_vad: dict[str, float] | None = None
            combined_vad: dict[str, float] | None = None

            # --- payload から VAD を読み取る（無ければ None） ---
            if isinstance(payload_obj, dict):
                baseline_vad = affect.extract_vad_from_payload_obj(payload_obj, "baseline_vad")
                shock_vad = affect.extract_vad_from_payload_obj(payload_obj, "shock_vad")

            # --- shock の時間減衰（半減期は affect 側の既定に従う） ---
            try:
                dt = int(now_ts) - int(st.last_confirmed_at)
            except Exception:  # noqa: BLE001
                dt = 0
            if dt < 0:
                dt = 0
            shock_halflife_seconds = 0
            if isinstance(payload_obj, dict):
                try:
                    shock_halflife_seconds = int(payload_obj.get("shock_halflife_seconds") or 0)
                except Exception:  # noqa: BLE001
                    shock_halflife_seconds = 0
            shock_decayed = affect.decay_shock_for_snapshot(
                shock_vad=shock_vad,
                dt_seconds=int(dt),
                shock_halflife_seconds=int(shock_halflife_seconds),
            )

            if baseline_vad is not None:
                combined_vad = affect.vad_add(baseline_vad, shock_decayed)

            return {
                "state_id": int(st.state_id),
                "kind": str(st.kind),
                "body_text": str(st.body_text),
                "payload": payload_obj,
                "vad": combined_vad,
                "baseline_vad": baseline_vad,
                "shock_vad": shock_decayed,
                "confidence": float(st.confidence),
                "salience": float(st.salience),
                "last_confirmed_at": format_iso8601_local(int(st.last_confirmed_at)),
                "valid_from_ts": (
                    format_iso8601_local(int(st.valid_from_ts)) if st.valid_from_ts is not None else None
                ),
                "valid_to_ts": format_iso8601_local(int(st.valid_to_ts)) if st.valid_to_ts is not None else None,
            }

    def _llm_io_loggers(self) -> tuple[logging.Logger, logging.Logger]:
        """LLM I/O ログの出力先ロガー（console/file）を返す。"""
        return (logging.getLogger("cocoro_ghost.llm_io.console"), logging.getLogger("cocoro_ghost.llm_io.file"))

    def _llm_log_level(self) -> str:
        """TOML設定に基づく LLMログレベル（DEBUG/INFO/OFF）を返す。"""
        return normalize_llm_log_level(self.config_store.toml_config.llm_log_level)

    def _llm_log_limits(self) -> tuple[int, int, int, int]:
        """TOML設定に基づく LLMログの文字数上限を返す。"""
        tc = self.config_store.toml_config
        return (
            int(tc.llm_log_console_max_chars),
            int(tc.llm_log_file_max_chars),
            int(tc.llm_log_console_value_max_chars),
            int(tc.llm_log_file_value_max_chars),
        )

    def _log_retrieval_debug(self, label: str, payload: Any) -> None:
        """検索（候補収集）まわりのデバッグ情報を LLM I/O ログへ出力する。"""

        # --- LLMログレベル（TOML）に従う ---
        llm_log_level = self._llm_log_level()
        if llm_log_level != "DEBUG":
            return

        # --- 出力先（console/file） ---
        console_logger, file_logger = self._llm_io_loggers()

        # --- トリミングはTOML設定に従う ---
        console_max_chars, file_max_chars, console_max_value_chars, file_max_value_chars = self._llm_log_limits()
        log_llm_payload(
            console_logger,
            label,
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(console_max_chars),
            max_value_chars=int(console_max_value_chars),
        )
        log_llm_payload(
            file_logger,
            label,
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(file_max_chars),
            max_value_chars=int(file_max_value_chars),
        )

    def _log_llm_stream_receive_complete(
        self,
        *,
        purpose: str,
        finish_reason: str,
        content: str,
        elapsed_ms: int,
    ) -> None:
        """ストリーミング応答の「受信完了」を LLM I/O ログへ出力する。"""

        # --- LLMログレベル（TOML）に従う ---
        llm_log_level = self._llm_log_level()
        if llm_log_level == "OFF":
            return

        # --- 出力先（console/file） ---
        console_logger, file_logger = self._llm_io_loggers()

        # --- 受信メタ（INFO） ---
        msg = "LLM response 受信 %s kind=chat stream=%s finish_reason=%s chars=%s ms=%s"
        args = (str(purpose), True, str(finish_reason or ""), len(content or ""), int(elapsed_ms))
        console_logger.info(msg, *args)
        file_logger.info(msg, *args)

        # --- 本文（DEBUGのみ） ---
        console_max_chars, file_max_chars, console_max_value_chars, file_max_value_chars = self._llm_log_limits()
        payload = {"finish_reason": str(finish_reason or ""), "content": str(content or "")}
        log_llm_payload(
            console_logger,
            "LLM response (chat)",
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(console_max_chars),
            max_value_chars=int(console_max_value_chars),
        )
        log_llm_payload(
            file_logger,
            "LLM response (chat)",
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(file_max_chars),
            max_value_chars=int(file_max_value_chars),
        )

    def stream_chat(self, request: schemas.ChatRequest, background_tasks: BackgroundTasks) -> Generator[str, None, None]:
        """チャットをSSEで返す（出来事ログ作成→検索→ストリーム→非同期更新）。"""

        # --- 設定を取得 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)

        # --- 記憶が無効なら、ベクトル索引/状態更新が育たない（初回だけ強くログする） ---
        global _warned_memory_disabled
        if not bool(self.config_store.memory_enabled) and not bool(_warned_memory_disabled):
            _warned_memory_disabled = True
            logger.warning("memory_enabled=false のため、非同期ジョブ（埋め込み/状態更新）は実行されません")

        # --- 入力を正規化 ---
        # NOTE:
        # - 端末を跨いでも同じ会話の続きになるよう、サーバ側の固定IDを使用する。
        # - 端末識別（WSの hello.client_id など）は別IDとして扱う。
        client_id = str(getattr(cfg, "shared_conversation_id", "") or "").strip()
        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"message": "shared_conversation_id が未設定です", "code": "server_misconfigured"},
            )
        input_text = str(request.input_text or "").strip()

        # --- 画像（data URI）を検証し、画像要約（内部用）を生成する ---
        raw_images = list(request.images or [])
        try:
            image_summaries, has_valid_image, _valid_images_data_uris = self._process_data_uri_images(
                raw_images=[str(x or "") for x in raw_images],
                purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_CHAT,
            )
        except HTTPException as exc:
            # NOTE: chat は SSE なので、400 は event:error として返す。
            detail = getattr(exc, "detail", None)
            if int(getattr(exc, "status_code", 0) or 0) == int(status.HTTP_400_BAD_REQUEST) and isinstance(detail, dict):
                yield _sse("error", detail)
                return
            raise

        # --- input_text が空の場合は、画像の有無で扱いを分ける ---
        if not input_text:
            if bool(has_valid_image):
                input_text = default_input_text_when_images_only()
            else:
                yield _sse(
                    "error",
                    {
                        "message": "input_text が空で、かつ有効な画像がありません",
                        "code": "invalid_request",
                    },
                )
                return

        # --- 検索/埋め込み向けに、画像要約（空でないもの）をクエリへ混ぜる ---
        non_empty_summaries = [s for s in image_summaries if str(s or "").strip()]
        augmented_query_text = str(input_text)
        if non_empty_summaries:
            augmented_query_text = (
                "\n\n".join(
                    [
                        str(input_text),
                        "[画像要約]",
                        "\n".join([str(s) for s in non_empty_summaries]),
                    ]
                )
                .strip()
            )

        now_ts = now_utc_ts()
        last_chat_created_at_ts: int | None = None
        reply_to_event_id: int | None = None

        # --- eventsを作成（ターン単位） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- 1) events を作る（assistant_text は後で埋める） ---
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=client_id,
                source="chat",
                user_text=input_text,
                assistant_text=None,
                image_summaries_json=(common_utils.json_dumps(image_summaries) if raw_images else None),
                entities_json="[]",
                client_context_json=(
                    common_utils.json_dumps(request.client_context) if request.client_context is not None else None
                ),
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

            # --- 2) reply_to（同じclient_idの直前チャット）を軽量に仮置きする ---
            prev = (
                db.query(Event.event_id, Event.created_at)
                .filter(Event.client_id == client_id)
                .filter(Event.source == "chat")
                .filter(Event.event_id != event_id)
                # assistant_text が無いターン（作成途中）は除外する
                .filter(Event.assistant_text.isnot(None))
                .order_by(Event.event_id.desc())
                .first()
            )
            if prev is not None and int(prev[0] or 0) > 0:
                # NOTE: 文脈グラフの本更新は非同期で行う。ここでは reply_to だけを即時に張る。
                from cocoro_ghost.memory_models import EventLink  # noqa: PLC0415

                reply_to_event_id = int(prev[0])
                db.add(
                    EventLink(
                        from_event_id=event_id,
                        to_event_id=int(prev[0]),
                        label="reply_to",
                        confidence=1.0,
                        evidence_event_ids_json="[]",
                        created_at=now_ts,
                    )
                )
                last_chat_created_at_ts = int(prev[1] or 0) if int(prev[1] or 0) > 0 else None

        # --- 3) 先行: 埋め込み取得（input_text のみ。SSE開始前の待ちを削る） ---
        # NOTE:
        # - 段階化（追加クエリの追い埋め込み）はしない（シンプル優先）。
        # - 先行埋め込みは vec検索（vector_recent/vector_global）で使う。
        # - 文字n-gram（trigram）は plan_obj.queries を補助的に使える。
        vector_embedding_future: concurrent.futures.Future[list[Any]] | None = None
        pre_ex: concurrent.futures.ThreadPoolExecutor | None = None
        try:
            pre_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            vector_embedding_future = pre_ex.submit(
                self.llm_client.generate_embedding,
                [str(augmented_query_text)],
                purpose=LlmRequestPurpose.SYNC_RETRIEVAL_QUERY_EMBEDDING,
            )
        finally:
            # NOTE: 送信済みタスクは継続する。ここで待たない（体感速度優先）。
            try:
                if pre_ex is not None:
                    pre_ex.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass

        # --- 4) RetrievalPlan（ルールベース。SearchPlan（LLM）は廃止） ---
        # NOTE:
        # - SearchPlan（LLM）を毎ターン呼ぶと、SSE開始前の同期待ちが増えるため廃止する。
        # - 代わりに、ユーザー入力から年/学生区分などの「明示ヒント」だけを軽く抽出して plan を作る。
        # - 上限（max_candidates）はTOML値を基準とし、実装側でも強制される。
        toml_max_candidates = int(self.config_store.toml_config.retrieval_max_candidates)
        plan_obj = _build_rule_based_retrieval_plan(
            user_input=str(input_text or ""),
            max_candidates=int(toml_max_candidates),
        )

        # --- 5) 候補収集（取りこぼし防止優先・可能なものは並列） ---
        candidates: list[_CandidateItem] = self._collect_candidates(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            input_text=augmented_query_text,
            plan_obj=plan_obj,
            vector_embedding_future=vector_embedding_future,
        )

        # --- 6) 選別（LLM → SearchResultPack） ---
        search_result_pack: dict[str, Any] = {"selected": []}
        try:
            # --- 選別入力は圧縮して渡す（体感速度優先） ---
            event_id_to_summary = _load_event_assistant_summaries_for_selection(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                candidates=candidates,
            )
            compact_candidates = _build_compact_candidates_for_selection(
                candidates, event_id_to_summary=event_id_to_summary
            )
            selection_input = common_utils.json_dumps(
                {
                    "user_input": input_text,
                    "image_summaries": list(non_empty_summaries),
                    "plan": plan_obj,
                    "candidates": compact_candidates,
                }
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "SearchResultPack selection input prepared candidates=%d chars=%d",
                    int(len(compact_candidates)),
                    int(len(selection_input)),
                )
            resp = self.llm_client.generate_json_response(
                system_prompt=prompt_builders.selection_system_prompt(),
                input_text=selection_input,
                purpose=LlmRequestPurpose.SYNC_SEARCH_SELECT,
                max_tokens=1500,
            )
            obj = common_utils.parse_first_json_object_or_none(common_utils.first_choice_content(resp))
            if obj is not None:
                search_result_pack = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearchResultPack selection failed; fallback to empty", exc_info=exc)

        # --- 7) retrieval_runs を記録（plan + candidate統計 + selected） ---
        candidates_log_obj = {
            "counts": {
                "total": len(candidates),
                "event": sum(1 for c in candidates if c.type == "event"),
                "state": sum(1 for c in candidates if c.type == "state"),
                "event_affect": sum(1 for c in candidates if c.type == "event_affect"),
            },
            "sources": {f"{c.type}:{c.id}": c.hit_sources for c in candidates},
        }
        if not isinstance(search_result_pack, dict) or not isinstance(search_result_pack.get("selected"), list):
            search_result_pack = {"selected": []}
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rr = RetrievalRun(
                event_id=int(event_id),
                created_at=now_ts,
                plan_json=common_utils.json_dumps(plan_obj),
                candidates_json=common_utils.json_dumps(candidates_log_obj),
                selected_json=common_utils.json_dumps(search_result_pack),
            )
            db.add(rr)

        # --- 8) 返答をSSEで生成（SearchResultPackを内部注入） ---
        system_prompt = prompt_builders.reply_system_prompt(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            second_person_label=cfg.second_person_label,
        )
        gap_seconds: int | None = None
        if last_chat_created_at_ts is not None and int(last_chat_created_at_ts) > 0:
            gap_seconds = int(now_ts) - int(last_chat_created_at_ts)
            if gap_seconds < 0:
                gap_seconds = 0

        internal_context = common_utils.json_dumps(
            {
                "TimeContext": {
                    "now": format_iso8601_local(int(now_ts)),
                    "last_chat_created_at": (
                        format_iso8601_local(int(last_chat_created_at_ts))
                        if last_chat_created_at_ts is not None and int(last_chat_created_at_ts) > 0
                        else None
                    ),
                    "gap_seconds": (int(gap_seconds) if gap_seconds is not None else None),
                },
                "LongMoodState": self._load_long_mood_state_snapshot(
                    embedding_preset_id=embedding_preset_id,
                    embedding_dimension=embedding_dimension,
                    now_ts=int(now_ts),
                ),
                "SearchResultPack": self._inflate_search_result_pack(
                    embedding_preset_id=embedding_preset_id,
                    embedding_dimension=embedding_dimension,
                    candidates=candidates,
                    pack=search_result_pack,
                ),
                "ImageSummaries": list(image_summaries),
            }
        )
        # --- 直近会話（短期コンテキスト）を付与して会話の安定性を上げる ---
        # NOTE:
        # - 記憶（長期）は SearchResultPack で注入する。
        # - 直近会話は「文脈の流れ（指示・口調・直前の合意）」のために常に少量入れる。
        # - max_turns_window は LLM プリセット（設定UI）側の値を使う（常に存在する前提）。
        recent_dialog = self._load_recent_chat_dialog_messages(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            client_id=client_id,
            exclude_event_id=int(event_id),
            max_turn_events=int(cfg.max_turns_window),
        )
        # --- 暗黙的キャッシュ（プロンプトキャッシュ）を効かせやすくする ---
        # NOTE:
        # - 「先頭側が同じほどキャッシュが効きやすい」前提で、system直後に固定ヘッダを置く。
        # - SearchResultPack/TimeContext はターンごとに変化しやすいので末尾側に寄せる。
        # - 直近会話は短期文脈として必要だが、先頭が揺れやすいので固定ヘッダの後ろへ置く。
        conversation: list[dict[str, str]] = []
        conversation.append(
            {
                "role": "assistant",
                "content": "\n".join(
                    [
                        "<<INTERNAL_CONTEXT>>",
                        "このメッセージは固定ヘッダ。本文に出力しない。",
                    ]
                ),
            }
        )
        conversation.extend(recent_dialog)
        conversation.append({"role": "assistant", "content": f"<<INTERNAL_CONTEXT>>\n{internal_context}"})
        conversation.append({"role": "user", "content": input_text})

        reply_text = ""
        finish_reason = ""
        sanitizer = _UserVisibleReplySanitizer()
        stream_failed = False
        stream_start = time.perf_counter()
        try:
            resp_stream = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.SYNC_CONVERSATION,
                stream=True,
            )
            for delta in self.llm_client.stream_delta_chunks(resp_stream):
                if delta.finish_reason:
                    finish_reason = str(delta.finish_reason)
                if not delta.text:
                    continue
                safe = sanitizer.feed(delta.text)
                if safe:
                    reply_text += safe
                    yield _sse("token", {"text": safe})
            tail = sanitizer.flush()
            if tail:
                reply_text += tail
                yield _sse("token", {"text": tail})
        except Exception as exc:  # noqa: BLE001
            stream_failed = True
            logger.error("chat stream failed", exc_info=exc)
            yield _sse("error", {"message": str(exc), "code": "llm_stream_failed"})
            return
        finally:
            # --- ストリーミング受信ログ（切断や例外時でも、可能な限り出す） ---
            if not stream_failed:
                elapsed_ms = int((time.perf_counter() - stream_start) * 1000)
                self._log_llm_stream_receive_complete(
                    purpose=LlmRequestPurpose.SYNC_CONVERSATION,
                    finish_reason=str(finish_reason or ""),
                    content=str(reply_text or ""),
                    elapsed_ms=int(elapsed_ms),
                )

        # --- 9) events を更新（assistant_text） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.execute(
                text("UPDATE events SET assistant_text=:t, updated_at=:u WHERE event_id=:id"),
                {"t": reply_text, "u": now_utc_ts(), "id": int(event_id)},
            )

        # --- 10) 非同期: 埋め込み更新ジョブを積む（次ターンで効く） ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 10.2) 非同期: アシスタント本文要約（選別入力の高速化） ---
        # NOTE:
        # - 返答本文（events.assistant_text）そのものは会話生成に使うので保持する。
        # - 要約は「選別（SearchResultPack）入力の軽量化」専用の派生情報として worker で作る。
        background_tasks.add_task(
            self._enqueue_event_assistant_summary_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 10.5) 非同期: 記憶更新（WritePlan） ---
        # NOTE:
        # - 返答とは別に「状態/感情/文脈/要約」を育てる。
        # - 体感速度を壊さないため、同期では行わない。
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 10.6) 非同期: 否定フィードバックによる自動分離（検索対象から除外） ---
        # NOTE:
        # - チャットターン開始（応答開始）を遅くしないため、レスポンス完了後に実行する。
        # - 事故率を下げるため、直前ターンの selected が1件のときだけ適用する（曖昧なら何もしない）。
        if reply_to_event_id is not None and self._should_auto_forget_for_feedback_text(str(input_text or "")):
            background_tasks.add_task(
                self._auto_forget_from_negative_feedback,
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                feedback_event_id=int(event_id),
                reply_to_event_id=int(reply_to_event_id),
                feedback_text=str(input_text),
            )

        # --- 11) SSE完了 ---
        yield _sse("done", {"event_id": int(event_id), "reply_text": reply_text, "usage": {"finish_reason": finish_reason}})

    # NOTE:
    # - 検索（候補収集/分散/pack生成）は `_ChatSearchMixin` に集約した。
    # - `_ChatMemoryMixin` には重複実装を置かない（MROで検索実装は一意）。
