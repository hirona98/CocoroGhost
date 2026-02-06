"""
Workerジョブハンドラディスパッチ

役割:
- jobs.kind と実処理ハンドラの対応付けだけを担当する。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.worker_handlers_embeddings import (
    _handle_upsert_event_affect_embedding,
    _handle_upsert_event_assistant_summary,
    _handle_upsert_event_embedding,
    _handle_upsert_state_embedding,
)
from cocoro_ghost.worker_handlers_maintenance import _handle_build_state_links, _handle_tidy_memory
from cocoro_ghost.worker_handlers_write_plan import _handle_apply_write_plan, _handle_generate_write_plan


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

    未知のkindは RuntimeError を送出する。
    """

    # --- kindごとにハンドラを呼ぶ ---
    if kind == "upsert_event_embedding":
        _handle_upsert_event_embedding(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )
        return

    if kind == "generate_write_plan":
        _handle_generate_write_plan(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )
        return

    if kind == "apply_write_plan":
        _handle_apply_write_plan(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            payload=payload,
        )
        return

    if kind == "upsert_state_embedding":
        _handle_upsert_state_embedding(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )
        return

    if kind == "upsert_event_affect_embedding":
        _handle_upsert_event_affect_embedding(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )
        return

    if kind == "upsert_event_assistant_summary":
        _handle_upsert_event_assistant_summary(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )
        return

    if kind == "tidy_memory":
        _handle_tidy_memory(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            payload=payload,
        )
        return

    if kind == "build_state_links":
        _handle_build_state_links(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )
        return

    raise RuntimeError(f"unknown job kind: {kind}")
