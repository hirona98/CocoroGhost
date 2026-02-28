"""
Worker ジョブハンドラ登録表。

目的:
    - jobs.kind と実処理ハンドラの対応を登録表で管理する。
    - `if kind == ...` の増殖を止める。
"""

from __future__ import annotations

from typing import Any, Callable

from cocoro_ghost.llm.client import LlmClient
from cocoro_ghost.jobs.handlers.autonomy import (
    _handle_deliberate_once,
    _handle_execute_intent,
    _handle_promote_action_result_to_searchable,
    _handle_snapshot_runtime,
    _handle_sweep_agent_jobs,
)
from cocoro_ghost.jobs.handlers.embeddings import (
    _handle_upsert_event_affect_embedding,
    _handle_upsert_event_assistant_summary,
    _handle_upsert_event_embedding,
    _handle_upsert_state_embedding,
)
from cocoro_ghost.jobs.handlers.maintenance import _handle_build_state_links, _handle_tidy_memory
from cocoro_ghost.jobs.handlers.write_plan import _handle_apply_write_plan, _handle_generate_write_plan


JobHandler = Callable[..., None]
RegisteredJobHandler = Callable[[str, int, LlmClient, dict[str, Any]], None]


def _run_with_llm(handler: JobHandler) -> RegisteredJobHandler:
    """
    `llm_client` を受け取る handler を統一契約へ包む。
    """

    # --- LLM を使う handler は llm_client 付きで呼ぶ ---
    def _wrapped(
        embedding_preset_id: str,
        embedding_dimension: int,
        llm_client: LlmClient,
        payload: dict[str, Any],
    ) -> None:
        handler(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )

    return _wrapped


def _run_without_llm(handler: JobHandler) -> RegisteredJobHandler:
    """
    `llm_client` を使わない handler を統一契約へ包む。
    """

    # --- LLM を使わない handler は必要な引数だけで呼ぶ ---
    def _wrapped(
        embedding_preset_id: str,
        embedding_dimension: int,
        llm_client: LlmClient,
        payload: dict[str, Any],
    ) -> None:
        del llm_client
        handler(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            payload=payload,
        )

    return _wrapped


_JOB_HANDLERS: dict[str, RegisteredJobHandler] = {
    "apply_write_plan": _run_without_llm(_handle_apply_write_plan),
    "build_state_links": _run_with_llm(_handle_build_state_links),
    "deliberate_once": _run_with_llm(_handle_deliberate_once),
    "execute_intent": _run_with_llm(_handle_execute_intent),
    "generate_write_plan": _run_with_llm(_handle_generate_write_plan),
    "promote_action_result_to_searchable": _run_without_llm(_handle_promote_action_result_to_searchable),
    "snapshot_runtime": _run_without_llm(_handle_snapshot_runtime),
    "sweep_agent_jobs": _run_without_llm(_handle_sweep_agent_jobs),
    "tidy_memory": _run_without_llm(_handle_tidy_memory),
    "upsert_event_affect_embedding": _run_with_llm(_handle_upsert_event_affect_embedding),
    "upsert_event_assistant_summary": _run_with_llm(_handle_upsert_event_assistant_summary),
    "upsert_event_embedding": _run_with_llm(_handle_upsert_event_embedding),
    "upsert_state_embedding": _run_with_llm(_handle_upsert_state_embedding),
}


def run_job_kind(
    *,
    kind: str,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """
    jobs.kind に対応する処理を実行する。

    未知の kind は RuntimeError を送出する。
    """

    # --- kind に対応する handler を引く ---
    handler = _JOB_HANDLERS.get(str(kind))
    if handler is None:
        raise RuntimeError(f"unknown job kind: {kind}")

    # --- すべての handler は同じキーワード引数契約で呼ぶ ---
    handler(embedding_preset_id, embedding_dimension, llm_client, payload)
