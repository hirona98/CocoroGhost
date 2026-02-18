"""
web_access capability。

目的:
    - 自発行動の `web_research` action を実行する。
    - `/api/chat` とは別経路でWeb検索を呼び出す。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost import common_utils, prompt_builders
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult, parse_capability_result
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose


def execute_web_research(
    *,
    llm_client: LlmClient,
    second_person_label: str,
    query: str,
    goal: str,
    constraints: list[str],
) -> CapabilityExecutionResult:
    """
    Web検索を実行し、CapabilityExecutionResult を返す。
    """

    # --- 入力を正規化 ---
    query_norm = str(query or "").strip()
    goal_norm = str(goal or "").strip()
    constraints_norm = [str(x or "").strip() for x in list(constraints or []) if str(x or "").strip()]

    # --- 空クエリは no_effect ---
    if not query_norm:
        return CapabilityExecutionResult(
            result_status="no_effect",
            summary="query が空のため実行しませんでした。",
            result_payload_json="{}",
            useful_for_recall_hint=0,
            next_trigger=None,
        )

    # --- system prompt を組み立て ---
    system_prompt = prompt_builders.autonomy_web_access_system_prompt(
        second_person_label=str(second_person_label),
    )
    input_obj = {
        "query": str(query_norm),
        "goal": str(goal_norm),
        "constraints": list(constraints_norm),
    }

    # --- Web検索付きLLM呼び出し ---
    try:
        resp = llm_client.generate_reply_response_with_web_search(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": common_utils.json_dumps(input_obj)}],
            purpose=LlmRequestPurpose.ASYNC_AUTONOMY_WEB_ACCESS,
            stream=False,
        )
        parsed = llm_client.response_json(resp)
        if not isinstance(parsed, dict):
            raise ValueError("web_access result is not a JSON object")
        return parse_capability_result(
            {
                "result_status": parsed.get("result_status"),
                "summary": parsed.get("summary"),
                "result_payload": {
                    "query": str(query_norm),
                    "goal": str(goal_norm),
                    "constraints": list(constraints_norm),
                    "findings": parsed.get("findings"),
                    "sources": parsed.get("sources"),
                    "notes": parsed.get("notes"),
                },
                "useful_for_recall_hint": bool(parsed.get("useful_for_recall_hint")),
                "next_trigger": parsed.get("next_trigger"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        # --- 失敗は result_status で返す（例外を外に漏らさない） ---
        return CapabilityExecutionResult(
            result_status="failed",
            summary=f"web_research 実行に失敗しました: {exc}",
            result_payload_json=common_utils.json_dumps(
                {
                    "query": str(query_norm),
                    "goal": str(goal_norm),
                    "constraints": list(constraints_norm),
                }
            ),
            useful_for_recall_hint=0,
            next_trigger=None,
        )
