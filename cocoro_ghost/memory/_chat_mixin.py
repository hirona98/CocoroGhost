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
import threading
import time
from typing import Any, Generator

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import text

from cocoro_ghost import schemas
from cocoro_ghost.core import affect
from cocoro_ghost.core import common_utils
from cocoro_ghost.llm import prompt_builders
from cocoro_ghost.storage import vector_index
from cocoro_ghost.storage.db import memory_session_scope
from cocoro_ghost.llm.debug import log_llm_payload, normalize_llm_log_level
from cocoro_ghost.llm.client import LlmRequestPurpose
from cocoro_ghost.memory._chat_search_mixin import _CandidateItem
from cocoro_ghost.memory._image_mixin import default_input_text_when_images_only
from cocoro_ghost.storage.memory_models import Event, EventAssistantSummary, EventLink, RetrievalRun, State, UserPreference
from cocoro_ghost.memory._utils import now_utc_ts
from cocoro_ghost.core.time_utils import format_iso8601_local


logger = logging.getLogger(__name__)
_warned_memory_disabled = False

# --- /api/chat 同時実行ガード ---
#
# 背景:
# - CocoroConsole（WPF）と CocoroGhost WebUI の両方から /api/chat が呼ばれ得る。
# - /api/chat は SSE で逐次返信するため、同時に複数走ると会話文脈が崩れやすく、UI側も破綻しやすい。
#
# 方針:
# - 1ユーザー前提の設計に合わせ、プロセス内で「同時に1本」だけ許可する。
# - 既に処理中の場合は、最も簡単な方法として SSE の `event:error` を返して終了する。
_chat_inflight_lock = threading.Lock()

# NOTE:
# - 検索ヘルパー（FTSクエリ生成/画像要約パース）は `_chat_search_mixin.py` に集約した。
# - `stream_chat` は `self._collect_candidates()` を呼び、検索自体は `_ChatSearchMixin` が担当する。
def _sse(event: str, data: dict) -> str:
    """SSEの1イベントを文字列化する。"""
    return f"event: {event}\n" + f"data: {common_utils.json_dumps(data)}\n\n"


def _humanize_gap_seconds_jp(gap_seconds: int | None) -> str | None:
    """
    経過秒数を「会話向けの日本語ラベル」に整形する。

    目的:
        - 内部コンテキスト（TimeContext）に、人間が扱いやすい形（gap_text）を同梱する。
        - 数値（秒）ではなく言語化された表現を渡し、本文の時間表現の整合性を上げる。

    Args:
        gap_seconds: 前回チャットからの経過秒数。

    Returns:
        例: "3分" / "1時間12分" / "20時間" / "1日3時間" など。
        不明の場合は None。
    """

    # --- 不明なら None ---
    if gap_seconds is None:
        return None

    # --- 0未満は 0 扱い（clock skew などの防御） ---
    s = int(gap_seconds)
    if s < 0:
        s = 0

    # --- 秒 ---
    if s < 60:
        return f"{s}秒"

    # --- 分 ---
    minutes = s // 60
    if s < 60 * 60:
        return f"{minutes}分"

    # --- 時間（必要なら分も付ける） ---
    hours = s // 3600
    rem_minutes = (s % 3600) // 60
    if s < 24 * 3600:
        # NOTE: 0分は省略して簡潔にする。
        return f"{hours}時間{rem_minutes}分" if rem_minutes else f"{hours}時間"

    # --- 日（必要なら時間も付ける） ---
    days = s // (24 * 3600)
    rem_hours = (s % (24 * 3600)) // 3600
    # NOTE: 0時間は省略して簡潔にする。
    return f"{days}日{rem_hours}時間" if rem_hours else f"{days}日"


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
    "entity_expand": "ex",
    "state_link_expand": "sl",
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
    persona_focus_hint: dict[str, Any] | None = None,
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
        "limits": {"max_candidates": int(max_candidates), "max_selected": 12},
    }
    # --- 人格の関心軸ヒント（ルールベース計画のまま補助情報として載せる） ---
    if isinstance(persona_focus_hint, dict) and persona_focus_hint:
        plan_obj["persona_focus_hint"] = dict(persona_focus_hint)
    return plan_obj


def _build_persona_focus_hint(
    *,
    confirmed_preferences_snapshot: dict[str, Any] | None,
    long_mood_state_snapshot: dict[str, Any] | None,
    persona_interest_state_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    RetrievalPlan/選別向けの PersonaFocusHint を作る。

    方針:
        - 文字列比較で persona 文を解析しない。
        - 構造化済みの confirmed preferences / mood snapshot を使う。
    """

    # --- confirmed preferences を安全に読む ---
    prefs = confirmed_preferences_snapshot if isinstance(confirmed_preferences_snapshot, dict) else {}
    topic_like = []
    style_like = []
    style_dislike = []
    try:
        topic_like = list((prefs.get("topic") or {}).get("like") or [])
        style_like = list((prefs.get("style") or {}).get("like") or [])
        style_dislike = list((prefs.get("style") or {}).get("dislike") or [])
    except Exception:  # noqa: BLE001
        topic_like = []
        style_like = []
        style_dislike = []

    # --- ラベルだけを短い配列へ正規化（subject を優先） ---
    def _subjects(rows: list[Any], *, cap: int = 6) -> list[str]:
        out: list[str] = []
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            subject = str(row.get("subject") or "").strip()
            if not subject:
                continue
            out.append(subject[:80])
            if len(out) >= int(cap):
                break
        return out

    # --- mood は構造情報のまま補助ヒントに載せる（意味判定はしない） ---
    mood_vad_hint = None
    if isinstance(long_mood_state_snapshot, dict):
        vad_obj = long_mood_state_snapshot.get("vad")
        if isinstance(vad_obj, dict):
            try:
                mood_vad_hint = {
                    "v": float(vad_obj.get("v")),
                    "a": float(vad_obj.get("a")),
                    "d": float(vad_obj.get("d")),
                }
            except Exception:  # noqa: BLE001
                mood_vad_hint = None

    # --- persona_interest_state は「現在の関心ターゲット」として補助ヒントに載せる ---
    interest_mode = None
    interest_targets: list[str] = []
    if isinstance(persona_interest_state_snapshot, dict):
        interest_mode_raw = str(persona_interest_state_snapshot.get("interaction_mode") or "").strip()
        interest_mode = str(interest_mode_raw) if interest_mode_raw else None
        targets_raw = persona_interest_state_snapshot.get("attention_targets")
        if isinstance(targets_raw, list):
            for t in list(targets_raw):
                if not isinstance(t, dict):
                    continue
                tt = str(t.get("type") or "").strip()
                tv = str(t.get("value") or "").strip()
                if not tt or not tv:
                    continue
                interest_targets.append(f"{tt}:{tv}"[:120])
                if len(interest_targets) >= 8:
                    break

    return {
        "topic_bias": _subjects(topic_like),
        "style_bias": _subjects(style_like),
        "avoid_bias": _subjects(style_dislike),
        "mood_vad_hint": mood_vad_hint,
        "interest_mode_hint": interest_mode,
        "interest_targets_hint": interest_targets,
    }


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


def _reorder_compact_candidates_for_persona_selection(
    compact_candidates: list[dict[str, Any]],
    *,
    persona_interest_state_snapshot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    SearchResultPack 選別入力の候補順を、persona_interest_state の構造情報で調整する。

    方針:
        - 自然言語本文の意味推定は行わない。
        - candidate の構造フィールド（t/src/k/hs）と interest_state の構造値だけ使う。
        - LLM選別を置き換えず、入力順の重み付けだけ行う。
    """

    # --- 入力の正規化 ---
    if not compact_candidates:
        return []
    if not isinstance(persona_interest_state_snapshot, dict):
        return list(compact_candidates)

    # --- interaction_mode（構造値） ---
    interaction_mode = str(persona_interest_state_snapshot.get("interaction_mode") or "").strip()
    if interaction_mode not in {"observe", "support", "explore", "wait"}:
        interaction_mode = "observe"

    # --- attention_targets の重みマップ化（構造フィールドのみ） ---
    targets_raw = persona_interest_state_snapshot.get("attention_targets")
    event_source_scores: dict[str, float] = {}
    state_kind_scores: dict[str, float] = {}
    world_capability_scores: dict[str, float] = {}
    world_entity_kind_scores: dict[str, float] = {}

    def _safe_weight(x: dict[str, Any]) -> float:
        try:
            return float(max(0.0, min(1.0, float(x.get("weight") or 0.0))))
        except Exception:  # noqa: BLE001
            return 0.0

    def _merge_score(dst: dict[str, float], key: str, weight: float) -> None:
        k = str(key or "").strip()
        if not k:
            return
        dst[k] = float(max(float(dst.get(k, 0.0)), float(weight)))

    if isinstance(targets_raw, list):
        for item in list(targets_raw):
            if not isinstance(item, dict):
                continue
            t = str(item.get("type") or "").strip()
            v = str(item.get("value") or "").strip()
            w = _safe_weight(item)
            if not v:
                continue
            if t == "event_source":
                _merge_score(event_source_scores, v, w)
                continue
            if t in {"state_kind", "kind"}:
                _merge_score(state_kind_scores, v, w)
                continue
            if t == "world_capability":
                _merge_score(world_capability_scores, v, w)
                continue
            if t == "world_entity_kind":
                _merge_score(world_entity_kind_scores, v, w)
                continue

    # --- world_* の構造ターゲットを会話候補の型/ヒット経路へ橋渡しする ---
    # NOTE:
    # - 本文比較はしないため、ここでは「どの種類の候補を前に出すか」だけ決める。
    research_interest_score = 0.0
    research_interest_score = max(research_interest_score, float(world_capability_scores.get("web_access") or 0.0))
    research_interest_score = max(
        research_interest_score,
        float(world_entity_kind_scores.get("web_research_query") or 0.0),
    )
    has_research_interest = research_interest_score > 0.0

    # --- interaction_mode ごとの型/経路バイアス（構造値のみ） ---
    def _candidate_score(c: dict[str, Any], original_index: int) -> tuple[float, int]:
        t = str(c.get("t") or "")
        src = str(c.get("src") or "")  # event 用
        state_kind = str(c.get("k") or "")  # state 用
        hs = [str(x or "").strip() for x in list(c.get("hs") or [])]

        score = 0.0

        # --- 明示ターゲット一致（event_source / state_kind） ---
        if t == "e" and src:
            score += float(event_source_scores.get(src) or 0.0) * 1.2
        if t == "s" and state_kind:
            score += float(state_kind_scores.get(state_kind) or 0.0) * 1.2

        # --- interaction_mode による型/経路重み ---
        if interaction_mode == "support":
            if t == "s":
                score += 0.25
            if any(h in {"reply_chain", "context_threads", "context_links"} for h in hs):
                score += 0.20
            if t == "e":
                score += 0.08
        elif interaction_mode == "explore":
            if t == "e":
                score += 0.20
            if any(h in {"vector_global", "vector_recent", "trigram_events", "about_time"} for h in hs):
                score += 0.18
        elif interaction_mode == "wait":
            if t == "s":
                score += 0.22
            if any(h in {"recent_events", "reply_chain", "context_threads", "context_links"} for h in hs):
                score += 0.12
            if t == "a":
                score -= 0.05
        else:  # observe
            if t == "e":
                score += 0.10
            if t == "s":
                score += 0.10

        # --- world_model_items 由来の research 関心は検索系経路/イベント候補を少し前へ ---
        if has_research_interest:
            if t == "e":
                score += 0.10 * float(max(0.0, min(1.0, research_interest_score)))
            if any(h in {"vector_global", "about_time", "trigram_events"} for h in hs):
                score += 0.12 * float(max(0.0, min(1.0, research_interest_score)))

        # --- 同点時は元の順序を維持（安定化） ---
        return (float(score), int(original_index))

    scored = []
    for idx, item in enumerate(list(compact_candidates or [])):
        if not isinstance(item, dict):
            continue
        scored.append((dict(item), _candidate_score(dict(item), idx)))

    scored.sort(key=lambda x: (-float(x[1][0]), int(x[1][1])))
    return [dict(x[0]) for x in scored]


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

            # --- event_affect は「内部の派生情報」なので、想起対象から外すだけにする ---
            # NOTE:
            # - 削除はしない（ログ/監査/デバッグ用に行は残す）。
            # - event_affect の候補収集は vec_items（ベクトル）由来なので、vec_items を消せば再想起されにくい。
            if t == "event_affect":
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
    ) -> tuple[list[dict[str, str]], list[int]]:
        """
        直近のチャット会話（短期コンテキスト）を messages 形式で返す。

        注意:
            - クライアントは単純I/Oなので、サーバ側で直近会話を付与して会話の安定性を上げる。
            - ここは「会話の流れ」を補助する目的。検索（記憶）は別経路（SearchResultPack）。
            - 直近ターンを SearchResultPack 側にも入れるとトークンが二重化しやすいので、
              ここで取得した event_id 群を「記憶検索から除外」する用途にも使う。
            - with を抜けても安全なように、ORMを返さず dict だけ返す。

        Returns:
            (messages, event_ids):
                - messages: OpenAI互換 messages（role/content）。
                - event_ids: messages に含まれた events.event_id の配列（古い順）。
        """
        cid = str(client_id or "").strip()
        if not cid:
            return [], []

        # --- 直近ターン数（短期コンテキスト） ---
        # NOTE: max_turns_window は常に設定される前提（欠損フォールバックはしない）。
        n = int(max_turn_events)
        if n <= 0:
            return [], []
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

        # --- event_id は「直近ターン」を特定するために返す ---
        # NOTE:
        # - SearchResultPack（記憶）側に、直近ターンが再注入されるとトークンが二重化しやすい。
        # - ここで返す event_ids を、候補収集（記憶検索）から除外する用途に使う。
        event_ids = [int(eid) for (eid, _, _) in rows if int(eid) > 0]

        out: list[dict[str, str]] = []
        # NOTE: メッセージは切り詰めず、そのまま送る。
        for _, ut, at in rows:
            if str(ut or "").strip():
                out.append({"role": "user", "content": str(ut)})
            if str(at or "").strip():
                out.append({"role": "assistant", "content": str(at)})
        return out, event_ids

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
                "last_confirmed_at": format_iso8601_local(int(st.last_confirmed_at)),
                "valid_from_ts": (
                    format_iso8601_local(int(st.valid_from_ts)) if st.valid_from_ts is not None else None
                ),
                "valid_to_ts": format_iso8601_local(int(st.valid_to_ts)) if st.valid_to_ts is not None else None,
            }

    def _load_confirmed_preferences_snapshot(
        self, *, embedding_preset_id: str, embedding_dimension: int
    ) -> dict[str, Any]:
        """
        確定した好み/苦手（user_preferences.status="confirmed"）のスナップショットを返す。

        目的:
            - 返答生成で「好き/苦手」を断定してよい根拠を、confirmed のみに限定する。
            - SearchResultPack（候補記憶）とは独立に注入し、好みの話題量を増やしやすくする。

        注意:
            - 1ユーザー前提のため client_id で分けない。
            - スナップショットは小さくし、SSE開始前の負荷と誤一般化を抑える（各カテゴリ上限あり）。
        """

        # --- 返却形（常に固定） ---
        out: dict[str, Any] = {
            "food": {"like": [], "dislike": []},
            "topic": {"like": [], "dislike": []},
            "style": {"like": [], "dislike": []},
        }

        # --- 上限（各domain×polarity） ---
        cap_per_bucket = 8

        # NOTE:
        # - session_scope は commit で ORM が expire され得るため、ORMインスタンスは外へ持ち出さない。
        # - 必要列だけをタプルで取り出し、セッション外でも安全に扱える形にする。
        rows: list[tuple[str, str, str, str | None, int | None, int, int]] = []
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            q = (
                db.query(
                    UserPreference.domain,
                    UserPreference.polarity,
                    UserPreference.subject_raw,
                    UserPreference.note,
                    UserPreference.confirmed_at,
                    UserPreference.last_seen_at,
                    UserPreference.id,
                )
                .filter(UserPreference.status == "confirmed")
                .order_by(UserPreference.confirmed_at.desc(), UserPreference.last_seen_at.desc(), UserPreference.id.desc())
            )
            rows = list(q.all())

        # --- domain/polarity で振り分け（Python側で上限を適用） ---
        counts: dict[tuple[str, str], int] = {}
        for r in rows:
            domain = str(r[0] or "").strip()
            polarity = str(r[1] or "").strip()
            if domain not in ("food", "topic", "style"):
                continue
            if polarity not in ("like", "dislike"):
                continue

            k = (domain, polarity)
            cur = int(counts.get(k, 0))
            if cur >= int(cap_per_bucket):
                continue

            subject = str(r[2] or "").strip()
            if not subject:
                continue
            note_s = str(r[3] or "").strip()
            out[domain][polarity].append({"subject": subject, "note": (note_s if note_s else None)})
            counts[k] = int(cur) + 1

        return out

    def _load_persona_interest_state_snapshot(
        self, *, embedding_preset_id: str, embedding_dimension: int
    ) -> dict[str, Any] | None:
        """
        current_thought_state の最新スナップショットを返す。

        目的:
            - 会話の選別/返答で、現在の関心・注目の継続状態を参照できるようにする。
            - 文字列比較ではなく構造化 payload を正として扱う。
        """

        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            st = (
                db.query(State)
                .filter(State.kind == "current_thought_state")
                .filter(State.searchable == 1)
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .first()
            )
            if st is None:
                return None

            payload_obj = common_utils.json_loads_maybe(str(st.payload_json or ""))
            if not isinstance(payload_obj, dict):
                payload_obj = {}

            attention_targets_out: list[dict[str, Any]] = []
            attention_targets_raw = payload_obj.get("attention_targets")
            if isinstance(attention_targets_raw, list):
                for item in list(attention_targets_raw):
                    if not isinstance(item, dict):
                        continue
                    target_type = str(item.get("type") or "").strip()
                    value = str(item.get("value") or "").strip()
                    if not target_type or not value:
                        continue
                    attention_targets_out.append(
                        {
                            "type": str(target_type),
                            "value": str(value),
                            "weight": float(item.get("weight") or 0.0),
                            "updated_at": (
                                format_iso8601_local(int(item.get("updated_at")))
                                if item.get("updated_at") is not None and int(item.get("updated_at") or 0) > 0
                                else None
                            ),
                        }
                    )
                    if len(attention_targets_out) >= 24:
                        break

            updated_from_event_ids = payload_obj.get("updated_from_event_ids")
            if not isinstance(updated_from_event_ids, list):
                updated_from_event_ids = []

            return {
                "state_id": int(st.state_id),
                "kind": str(st.kind),
                "body_text": str(st.body_text or ""),
                "interaction_mode": str(payload_obj.get("interaction_mode") or "").strip() or None,
                "active_thread_id": str(payload_obj.get("active_thread_id") or "").strip() or None,
                "focus_summary": str(payload_obj.get("focus_summary") or "").strip() or None,
                "next_candidate_action": (
                    dict(payload_obj.get("next_candidate_action") or {})
                    if isinstance(payload_obj.get("next_candidate_action"), dict)
                    else None
                ),
                "attention_targets": attention_targets_out,
                "updated_from_event_ids": [
                    int(x) for x in list(updated_from_event_ids) if isinstance(x, (int, float)) and int(x) > 0
                ][:20],
                "updated_from": (payload_obj.get("updated_from") if isinstance(payload_obj.get("updated_from"), dict) else None),
                "confidence": float(st.confidence),
                "last_confirmed_at": format_iso8601_local(int(st.last_confirmed_at)),
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
        """
        チャットをSSEで返す（出来事ログ作成→検索→ストリーム→非同期更新）。

        注意:
            - WebUI/Console など複数経路から同時に /api/chat が呼ばれ得るため、
              プロセス内で「同時に1本」だけ許可する。
            - 多重送信を受けた場合は、最も簡単な方法として SSE の `event:error` を返して終了する。
        """

        # --- 同時実行ガード（WebUI/Consoleの二重送信を抑止） ---
        # NOTE:
        # - acquire が成功した場合は、generator の終了/切断時に finally で必ず解放される。
        # - 既に処理中の場合は待たずに SSE error を返す（最短で終わらせる）。
        if not _chat_inflight_lock.acquire(blocking=False):
            yield _sse(
                "error",
                {
                    "message": "他のチャット処理中です。応答が完了してから再送してください。",
                    "code": "chat_busy",
                },
            )
            return

        try:
            # --- 本体処理 ---
            yield from self._stream_chat_unlocked(request=request, background_tasks=background_tasks)
        finally:
            # --- 解放（クライアント切断時も含めて必ず実行される） ---
            _chat_inflight_lock.release()

    def _stream_chat_unlocked(
        self, request: schemas.ChatRequest, background_tasks: BackgroundTasks
    ) -> Generator[str, None, None]:
        """
        チャットをSSEで返す（出来事ログ作成→検索→ストリーム→非同期更新）。

        注意:
            - 同時実行ガード（ロック）は呼び出し側（`stream_chat`）で行う。
            - このメソッドは「本体処理」だけを担当する。
        """

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
                from cocoro_ghost.storage.memory_models import EventLink  # noqa: PLC0415

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

        # --- 2.5) 直近会話（短期コンテキスト）を先にロードする ---
        # NOTE:
        # - 直近会話は「会話の流れ（指示・口調・直前の合意）」のために常に少量入れる。
        # - 一方で、同じ直近会話が SearchResultPack（記憶）側にも入るとトークンが二重化しやすい。
        # - そのため、ここで取得した event_id 群は「記憶検索から除外」して重複を避ける。
        recent_dialog, recent_dialog_event_ids = self._load_recent_chat_dialog_messages(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            client_id=client_id,
            exclude_event_id=int(event_id),
            max_turn_events=int(cfg.max_turns_window),
        )
        exclude_recent_event_ids_for_memory = {int(x) for x in (recent_dialog_event_ids or []) if int(x) > 0}
        exclude_recent_event_ids_for_memory.add(int(event_id))

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
        # --- 会話選別/返答で共通に使う人格スナップショットを先に取得する ---
        long_mood_state_snapshot = self._load_long_mood_state_snapshot(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            now_ts=int(now_ts),
        )
        confirmed_preferences_snapshot = self._load_confirmed_preferences_snapshot(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
        )
        persona_interest_state_snapshot = self._load_persona_interest_state_snapshot(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
        )
        persona_focus_hint = _build_persona_focus_hint(
            confirmed_preferences_snapshot=confirmed_preferences_snapshot,
            long_mood_state_snapshot=long_mood_state_snapshot,
            persona_interest_state_snapshot=persona_interest_state_snapshot,
        )
        toml_max_candidates = int(self.config_store.toml_config.retrieval_max_candidates)
        plan_obj = _build_rule_based_retrieval_plan(
            user_input=str(input_text or ""),
            max_candidates=int(toml_max_candidates),
            persona_focus_hint=dict(persona_focus_hint or {}),
        )

        # --- 5) 候補収集（取りこぼし防止優先・可能なものは並列） ---
        candidates, candidates_collect_debug = self._collect_candidates(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            input_text=augmented_query_text,
            plan_obj=plan_obj,
            vector_embedding_future=vector_embedding_future,
            exclude_event_ids=exclude_recent_event_ids_for_memory,
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
            # --- 選別入力順を人格の関心状態（構造値）で調整する ---
            compact_candidates = _reorder_compact_candidates_for_persona_selection(
                compact_candidates,
                persona_interest_state_snapshot=persona_interest_state_snapshot,
            )
            selection_input = common_utils.json_dumps(
                {
                    "user_input": input_text,
                    "image_summaries": list(non_empty_summaries),
                    "plan": plan_obj,
                    "persona_selection_context": {
                        "second_person_label": str(cfg.second_person_label or "").strip() or "あなた",
                        "persona_text": str(cfg.persona_text or ""),
                        "addon_text": str(cfg.addon_text or ""),
                        "long_mood_state": long_mood_state_snapshot,
                        "confirmed_preferences": confirmed_preferences_snapshot,
                        "current_thought": persona_interest_state_snapshot,
                    },
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
            "collect_debug": (candidates_collect_debug or {}),
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
        gap_text: str | None = None
        if last_chat_created_at_ts is not None and int(last_chat_created_at_ts) > 0:
            gap_seconds = int(now_ts) - int(last_chat_created_at_ts)
            if gap_seconds < 0:
                gap_seconds = 0
        # --- 経過時間を会話向けの短い日本語にする（LLMの誤認防止） ---
        gap_text = _humanize_gap_seconds_jp(gap_seconds)

        internal_context = common_utils.json_dumps(
            {
                "TimeContext": {
                    "now": format_iso8601_local(int(now_ts)),
                    "last_chat_created_at": (
                        format_iso8601_local(int(last_chat_created_at_ts))
                        if last_chat_created_at_ts is not None and int(last_chat_created_at_ts) > 0
                        else None
                    ),
                    "gap_text": gap_text,
                },
                "LongMoodState": long_mood_state_snapshot,
                "ConfirmedPreferences": confirmed_preferences_snapshot,
                "CurrentThoughtState": persona_interest_state_snapshot,
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
        # - recent_dialog は 2.5) でロード済み（DB二重読みを避ける）。
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

        # --- 10.7) 非同期: 自発行動トリガ（event） ---
        background_tasks.add_task(
            self._enqueue_autonomy_event_trigger,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            source="chat",
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
