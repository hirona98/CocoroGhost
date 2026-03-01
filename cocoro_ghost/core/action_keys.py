"""
自発行動の同一性判定に使う action key ヘルパ。

役割:
    - action_payload の構造値から、同一行動を比較する正規化キーを作る。
    - Deliberation / Intent投入 / DBマイグレーションで同じ判定基準を共有する。
"""

from __future__ import annotations

from typing import Any


def normalize_action_text(value: Any) -> str:
    """
    行動キー比較用に文字列を正規化する。

    方針:
        - 改行や余分な空白を畳み、比較ゆらぎを減らす。
        - 英字は casefold して、大文字小文字差を吸収する。
    """

    # --- 空文字を正規化する ---
    text_in = str(value or "").replace("\r", "\n").strip()
    if not text_in:
        return ""

    # --- 連続空白を1つに畳む ---
    compact = " ".join(text_in.split())
    return str(compact).casefold()


def canonical_action_key(
    *,
    action_type: str,
    action_payload: dict[str, Any] | None,
) -> str:
    """
    同一行動を判定するための正規化キーを返す。

    方針:
        - 自然言語本文ではなく、構造化 payload だけで同一性を決める。
        - query のような再実行しやすい行動は、入力値まで含めて識別する。
    """

    # --- 行動種別を正規化する ---
    action_type_norm = str(action_type or "").strip()
    payload_obj = dict(action_payload or {})
    if not action_type_norm:
        return ""

    # --- web_research は query を主キーにする ---
    if action_type_norm == "web_research":
        query_norm = normalize_action_text(payload_obj.get("query"))
        if query_norm:
            return f"web_research:{query_norm}"
        goal_norm = normalize_action_text(payload_obj.get("goal"))
        if goal_norm:
            return f"web_research_goal:{goal_norm}"
        return "web_research"

    # --- 観測系は target_client_id まで含める ---
    if action_type_norm in {"observe_screen", "observe_camera"}:
        target_client_id = normalize_action_text(payload_obj.get("target_client_id"))
        if target_client_id:
            return f"{action_type_norm}:{target_client_id}"
        return str(action_type_norm)

    # --- それ以外は action_type 単位で扱う ---
    return str(action_type_norm)
