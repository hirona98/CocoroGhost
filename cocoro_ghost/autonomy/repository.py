"""
autonomy 用DBアクセス。

目的:
    - Trigger/Intent の主要操作を1箇所に集約する。
    - Orchestrator と Worker で同じ保存契約を使う。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from cocoro_ghost import common_utils
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import Job


_JOB_PENDING = 0


@dataclass(frozen=True)
class ClaimedTrigger:
    """claim 済み trigger の軽量値オブジェクト。"""

    trigger_id: str
    claim_token: str


class AutonomyRepository:
    """autonomy テーブル専用のリポジトリ。"""

    def __init__(self, *, embedding_preset_id: str, embedding_dimension: int) -> None:
        self.embedding_preset_id = str(embedding_preset_id)
        self.embedding_dimension = int(embedding_dimension)

    def enqueue_trigger(
        self,
        *,
        trigger_type: str,
        trigger_key: str,
        payload: dict[str, Any] | None,
        now_system_ts: int,
        scheduled_at: int | None,
        source_event_id: int | None,
    ) -> bool:
        """
        autonomy_triggers へ queued trigger を追加する。

        Returns:
            True: 新規挿入
            False: 既存（重複排除）
        """

        # --- 値を正規化 ---
        trigger_id = str(uuid.uuid4())
        payload_json = common_utils.json_dumps(payload or {})

        # --- 部分一意制約（queued/claimed）に従って INSERT OR IGNORE ---
        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            row = db.execute(
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
                        :trigger_type,
                        :trigger_key,
                        :source_event_id,
                        :payload_json,
                        'queued',
                        :scheduled_at,
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
                    "trigger_id": str(trigger_id),
                    "trigger_type": str(trigger_type),
                    "trigger_key": str(trigger_key),
                    "source_event_id": (int(source_event_id) if source_event_id is not None else None),
                    "payload_json": str(payload_json),
                    "scheduled_at": (int(scheduled_at) if scheduled_at is not None else None),
                    "created_at": int(now_system_ts),
                    "updated_at": int(now_system_ts),
                },
            )
            return int(row.rowcount or 0) > 0

    def claim_due_triggers(
        self,
        *,
        now_domain_ts: int,
        now_system_ts: int,
        limit: int,
    ) -> list[ClaimedTrigger]:
        """
        due な queued trigger を claimed に遷移し、実行対象を返す。
        """

        out: list[ClaimedTrigger] = []
        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            rows = db.execute(
                text(
                    """
                    SELECT trigger_id
                      FROM autonomy_triggers
                     WHERE status = 'queued'
                       AND (scheduled_at IS NULL OR scheduled_at <= :now_domain_ts)
                     ORDER BY COALESCE(scheduled_at, 0) ASC, created_at ASC
                     LIMIT :limit
                    """
                ),
                {
                    "now_domain_ts": int(now_domain_ts),
                    "limit": int(max(1, int(limit))),
                },
            ).fetchall()
            for row in rows:
                trigger_id = str(row[0] or "").strip()
                if not trigger_id:
                    continue
                claim_token = str(uuid.uuid4())
                claimed = db.execute(
                    text(
                        """
                        UPDATE autonomy_triggers
                           SET status='claimed',
                               claim_token=:claim_token,
                               claimed_at=:claimed_at,
                               attempts=attempts+1,
                               updated_at=:updated_at
                         WHERE trigger_id=:trigger_id
                           AND status='queued'
                        """
                    ),
                    {
                        "claim_token": str(claim_token),
                        "claimed_at": int(now_system_ts),
                        "updated_at": int(now_system_ts),
                        "trigger_id": str(trigger_id),
                    },
                )
                if int(claimed.rowcount or 0) != 1:
                    continue
                out.append(ClaimedTrigger(trigger_id=str(trigger_id), claim_token=str(claim_token)))
        return out

    def enqueue_job(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        run_after_system_ts: int,
        now_system_ts: int,
    ) -> None:
        """
        jobs テーブルへ pending ジョブを1件追加する。
        """

        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            db.add(
                Job(
                    kind=str(kind),
                    payload_json=common_utils.json_dumps(payload),
                    status=int(_JOB_PENDING),
                    run_after=int(run_after_system_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_system_ts),
                    updated_at=int(now_system_ts),
                )
            )

    def get_status_counts(self, *, now_domain_ts: int) -> dict[str, int]:
        """
        autonomy の主要滞留数を返す。
        """

        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            row = db.execute(
                text(
                    """
                    SELECT
                        SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                        SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) AS claimed,
                        SUM(CASE WHEN status='queued' AND (scheduled_at IS NULL OR scheduled_at <= :now_domain_ts) THEN 1 ELSE 0 END) AS due
                      FROM autonomy_triggers
                    """
                ),
                {"now_domain_ts": int(now_domain_ts)},
            ).fetchone()
            row_intents = db.execute(
                text(
                    """
                    SELECT
                        SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                        SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                        SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked
                      FROM intents
                    """
                )
            ).fetchone()

            return {
                "triggers_queued": int((row[0] if row and row[0] is not None else 0) or 0),
                "triggers_claimed": int((row[1] if row and row[1] is not None else 0) or 0),
                "triggers_due": int((row[2] if row and row[2] is not None else 0) or 0),
                "intents_queued": int((row_intents[0] if row_intents and row_intents[0] is not None else 0) or 0),
                "intents_running": int((row_intents[1] if row_intents and row_intents[1] is not None else 0) or 0),
                "intents_blocked": int((row_intents[2] if row_intents and row_intents[2] is not None else 0) or 0),
            }

    def list_recent_intents(self, *, limit: int) -> list[dict[str, Any]]:
        """
        Intent 一覧（新しい順）を返す。
        """

        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            rows = db.execute(
                text(
                    """
                    SELECT intent_id, decision_id, action_type, status, priority, scheduled_at, blocked_reason, dropped_reason, updated_at
                      FROM intents
                     ORDER BY updated_at DESC, created_at DESC
                     LIMIT :limit
                    """
                ),
                {"limit": int(max(1, int(limit)))},
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                out.append(
                    {
                        "intent_id": str(row[0]),
                        "decision_id": str(row[1]),
                        "action_type": str(row[2]),
                        "status": str(row[3]),
                        "priority": int(row[4] or 0),
                        "scheduled_at": (int(row[5]) if row[5] is not None else None),
                        "blocked_reason": (str(row[6]) if row[6] is not None else None),
                        "dropped_reason": (str(row[7]) if row[7] is not None else None),
                        "updated_at": int(row[8] or 0),
                    }
                )
            return out
