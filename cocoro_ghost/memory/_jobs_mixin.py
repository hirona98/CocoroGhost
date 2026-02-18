"""
memory配下: ジョブ投入（mixin）

目的:
    - 非同期処理（埋め込み更新/WritePlan生成）の enqueue を1箇所に集約する。
    - chat/notification/meta_request 等の複数経路から同じジョブを積めるようにする。
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from cocoro_ghost import common_utils
from cocoro_ghost.db import load_global_settings, memory_session_scope, settings_session_scope
from cocoro_ghost.memory._utils import now_system_utc_ts
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
        now_ts = now_system_utc_ts()
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
        now_ts = now_system_utc_ts()
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
        now_ts = now_system_utc_ts()
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

    def _enqueue_autonomy_event_trigger(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        event_id: int,
        source: str,
    ) -> None:
        """
        自発行動用の event trigger を queued 追加する。

        注意:
            - 重複排除は autonomy_triggers の部分一意制約（trigger_key）で行う。
            - autonomy が OFF の時は積まない。
        """

        # --- memory_enabled を確認 ---
        if not bool(self.config_store.memory_enabled):  # type: ignore[attr-defined]
            return

        # --- autonomy_enabled は settings.db を正として評価する ---
        with settings_session_scope() as settings_db:
            global_settings = load_global_settings(settings_db)
            if not bool(getattr(global_settings, "autonomy_enabled", False)):
                return

        # --- トリガを queued 追加 ---
        now_system_ts = now_system_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.execute(
                text(
                    """
                    INSERT OR IGNORE INTO autonomy_triggers(
                        trigger_id,
                        trigger_type,
                        trigger_key,
                        source_event_id,
                        payload_json,
                        status,
                        scheduled_at,
                        claim_token,
                        claimed_at,
                        attempts,
                        last_error,
                        dropped_reason,
                        dropped_at,
                        created_at,
                        updated_at
                    )
                    VALUES(
                        :trigger_id,
                        'event',
                        :trigger_key,
                        :source_event_id,
                        :payload_json,
                        'queued',
                        NULL,
                        NULL,
                        NULL,
                        0,
                        NULL,
                        '',
                        NULL,
                        :created_at,
                        :updated_at
                    )
                    """
                ),
                {
                    "trigger_id": str(uuid.uuid4()),
                    "trigger_key": f"event:{int(event_id)}",
                    "source_event_id": int(event_id),
                    "payload_json": common_utils.json_dumps({"source": str(source)}),
                    "created_at": int(now_system_ts),
                    "updated_at": int(now_system_ts),
                },
            )
