"""
Workerジョブハンドラ（自律ループ）。

役割:
- run_autonomy_cycle ジョブを実行する。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost.autonomy.loop_runtime import run_autonomy_cycle
from cocoro_ghost.llm_client import LlmClient


def _handle_run_autonomy_cycle(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """自律ループを1サイクル実行する。"""

    # --- ループ本体を実行 ---
    _ = run_autonomy_cycle(
        embedding_preset_id=str(embedding_preset_id),
        embedding_dimension=int(embedding_dimension),
        llm_client=llm_client,
        payload=dict(payload or {}),
    )
