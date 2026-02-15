"""
自律ループの戦術決定（Tacticalize）。

役割:
- trigger/観測内容から次の ActionTicket 計画を決定する。
- loop_runtime 本体へ capability 固有分岐を持ち込まない。
"""

from __future__ import annotations

import re
from typing import Any

from cocoro_ghost.autonomy.preconditions import (
    PRECONDITION_SOURCE_TEXT_PRESENT,
    PRECONDITION_SOURCE_URL_PRESENT,
    PRECONDITION_URL_PRESENT,
)


def _extract_primary_user_text(source_event: dict[str, Any] | None) -> str:
    """戦術判断に使う主入力テキストを返す。"""

    # --- event が無い場合は空を返す ---
    if source_event is None:
        return ""

    # --- user_text を優先して使う ---
    user_text = str(source_event.get("user_text") or "").strip()
    if user_text:
        return user_text

    # --- 無ければ assistant_text を使う ---
    return str(source_event.get("assistant_text") or "").strip()


def _extract_first_url(text: str) -> str | None:
    """テキストから最初のURLを抽出する。"""

    # --- http/https URL を抽出 ---
    m = re.search(r"(https?://[^\s<>\")']+)", str(text or "").strip())
    if not m:
        return None
    return str(m.group(1) or "").strip() or None


def _should_search_web(text: str) -> bool:
    """Web検索を行うべき入力かを判定する。"""

    # --- 検索意図キーワードを判定 ---
    text_s = str(text or "").strip()
    if not text_s:
        return False
    keywords = ["検索", "調べて", "調査", "search", "web", "ウェブ"]
    return any(kw in text_s for kw in keywords)


def _build_web_search_query(text: str) -> str:
    """検索クエリを入力テキストから構築する。"""

    # --- 代表的な接頭辞を除去 ---
    q = str(text or "").strip()
    prefixes = ["検索:", "検索：", "調べて:", "調べて：", "search:"]
    for p in prefixes:
        if q.startswith(p):
            q = q[len(p) :].strip()
            break

    # --- 空になった場合は元文を使う ---
    if not q:
        return str(text or "").strip()
    return q


def _should_extract_structured(text: str) -> bool:
    """構造化抽出を行うべき入力かを判定する。"""

    # --- 構造化抽出意図キーワードを判定 ---
    text_s = str(text or "").strip()
    if not text_s:
        return False
    keywords = ["構造化", "extract_structured", "抽出して", "extract"]
    return any(kw in text_s for kw in keywords)


def _extract_source_text(text: str) -> str:
    """入力テキストから source_text を抽出する。"""

    # --- source_text: 形式を抽出 ---
    m1 = re.search(r"source_text\s*[:：=]\s*(.+)$", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    if m1:
        return str(m1.group(1) or "").strip()
    return ""


def _extract_fields(text: str) -> list[str]:
    """入力テキストから構造化対象フィールド一覧を抽出する。"""

    # --- fields: a,b,c 形式を抽出 ---
    m1 = re.search(r"fields\s*[:：=]\s*(.+)$", str(text or ""), flags=re.IGNORECASE)
    if not m1:
        return []
    raw = str(m1.group(1) or "").strip()
    out = [str(x).strip() for x in raw.split(",") if str(x).strip()]
    return list(dict.fromkeys(out))


def _build_target_schema_json(fields: list[str]) -> dict[str, Any]:
    """抽出対象フィールド一覧から target_schema_json を生成する。"""

    # --- properties を構築 ---
    properties: dict[str, Any] = {}
    for key in list(fields or []):
        key_s = str(key).strip()
        if not key_s:
            continue
        properties[key_s] = {"type": "string"}
    return {"type": "object", "properties": properties}


def _build_speak_message(*, trigger_type: str, source_event: dict[str, Any] | None, observation_text: str) -> str:
    """`speak.emit` 用メッセージを構築する。"""

    # --- event 起点なら観測内容を短く伝える ---
    if source_event is not None:
        message = f"いま考えていること: {observation_text}"
        return message[:300]

    # --- system/action_result 起点の既定文 ---
    if trigger_type == "action_result":
        return "さっきの行動結果をふまえて、次の方針を考えてるよ。"
    if trigger_type == "startup":
        return "起動したので、まず周囲の状況を観測するね。"
    return "いまの状況を観測して、次の行動を考えてるよ。"


def decide_tactical_plan(
    *,
    trigger_type: str,
    source_event: dict[str, Any] | None,
    observation_text: str,
) -> dict[str, Any]:
    """Tacticalize の最小決定を返す。"""

    # --- event_created 以外は speak を使う ---
    if str(trigger_type) != "event_created":
        speak_message = _build_speak_message(
            trigger_type=trigger_type,
            source_event=source_event,
            observation_text=observation_text,
        )
        return {
            "goal_id": "autonomy_speak_observation",
            "goal_title": "観測を言語化する",
            "goal_intent": f"trigger_type={trigger_type} で最新観測を言語化する",
            "success_criteria": ["speak.emit を1回実行する"],
            "capability_id": "speak",
            "operation": "emit",
            "input_payload": {"message": str(speak_message)},
            "preconditions": [],
            "expected_effect": ["event.persisted"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- event入力本文を取得 ---
    input_text = _extract_primary_user_text(source_event)

    # --- 構造化抽出意図を含む場合は extract_structured を優先 ---
    if _should_extract_structured(input_text):
        source_url = _extract_first_url(input_text)
        source_text = _extract_source_text(input_text)
        fields = _extract_fields(input_text)
        if not fields:
            fields = ["title", "summary"]
        return {
            "goal_id": "autonomy_web_access",
            "goal_title": "観測を構造化情報へ変換する",
            "goal_intent": f"trigger_type={trigger_type} で構造化抽出を実行する",
            "success_criteria": ["web_access.extract_structured を1回実行する"],
            "capability_id": "web_access",
            "operation": "extract_structured",
            "input_payload": {
                "source_url": (str(source_url) if source_url is not None else ""),
                "source_text": str(source_text),
                "target_schema_json": _build_target_schema_json(fields),
            },
            "preconditions": [PRECONDITION_SOURCE_URL_PRESENT, PRECONDITION_SOURCE_TEXT_PRESENT],
            "expected_effect": ["web.structured_extracted"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- URLを含む場合は open_url を優先 ---
    url = _extract_first_url(input_text)
    if url is not None:
        return {
            "goal_id": "autonomy_web_access",
            "goal_title": "観測をWeb参照で具体化する",
            "goal_intent": f"trigger_type={trigger_type} でURLを参照する",
            "success_criteria": ["web_access.open_url を1回実行する"],
            "capability_id": "web_access",
            "operation": "open_url",
            "input_payload": {"url": str(url), "max_chars": 8000},
            "preconditions": [PRECONDITION_URL_PRESENT],
            "expected_effect": ["web.page_opened"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- 検索意図を含む場合は search を使う ---
    if _should_search_web(input_text):
        query = _build_web_search_query(input_text)
        return {
            "goal_id": "autonomy_web_access",
            "goal_title": "観測をWeb検索で具体化する",
            "goal_intent": f"trigger_type={trigger_type} でWeb検索を実行する",
            "success_criteria": ["web_access.search を1回実行する"],
            "capability_id": "web_access",
            "operation": "search",
            "input_payload": {
                "query": str(query),
                "top_k": 5,
                "recency_days": None,
                "domains": [],
                "locale": "ja-JP",
            },
            "preconditions": [],
            "expected_effect": ["web.search_result"],
            "verify": ["wm_action_results.status=succeeded"],
        }

    # --- それ以外は speak を使う ---
    speak_message = _build_speak_message(
        trigger_type=trigger_type,
        source_event=source_event,
        observation_text=observation_text,
    )
    return {
        "goal_id": "autonomy_speak_observation",
        "goal_title": "観測を言語化する",
        "goal_intent": f"trigger_type={trigger_type} で最新観測を言語化する",
        "success_criteria": ["speak.emit を1回実行する"],
        "capability_id": "speak",
        "operation": "emit",
        "input_payload": {"message": str(speak_message)},
        "preconditions": [],
        "expected_effect": ["event.persisted"],
        "verify": ["wm_action_results.status=succeeded"],
    }
