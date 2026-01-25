"""
memory配下: ジョブ投入（mixin）

目的:
    - 非同期処理（埋め込み更新/WritePlan生成）の enqueue を1箇所に集約する。
    - chat/notification/meta_request 等の複数経路から同じジョブを積めるようにする。
"""

from __future__ import annotations

from cocoro_ghost import common_utils
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory._utils import now_utc_ts
from cocoro_ghost.memory_models import Job


_JOB_PENDING = 0


class _JobsMemoryMixin:
    """ジョブ投入系の実装（mixin）。"""

    def _enqueue_event_embedding_job(self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
        """出来事ログの埋め込み更新ジョブを積む（次ターンで効かせる）。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):  # type: ignore[attr-defined]
            return

        # --- jobs に投入 ---
        now_ts = now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="upsert_event_embedding",
                    payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    def _enqueue_write_plan_job(self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
        """記憶更新のための WritePlan 生成ジョブを積む。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):  # type: ignore[attr-defined]
            return

        # --- jobs に投入 ---
        now_ts = now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="generate_write_plan",
                    payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    def _enqueue_event_assistant_summary_job(
        self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int
    ) -> None:
        """イベントのアシスタント本文要約ジョブを積む（SearchResultPack選別の高速化）。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):  # type: ignore[attr-defined]
            return

        # --- jobs に投入 ---
        now_ts = now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="upsert_event_assistant_summary",
                    payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )
