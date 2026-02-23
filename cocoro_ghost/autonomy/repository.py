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

from sqlalchemy import bindparam, text

from cocoro_ghost import common_utils
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import AgentJob, Intent, Job, RuntimeSnapshot


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

    def claim_agent_jobs(
        self,
        *,
        runner_id: str,
        backends: list[str],
        now_system_ts: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        queued な agent_jobs を claimed に遷移し、runner 向けスナップショットを返す。
        """

        # --- 入力を正規化 ---
        runner_id_norm = str(runner_id or "").strip()
        if not runner_id_norm:
            return []
        backend_list = [str(x or "").strip() for x in list(backends or []) if str(x or "").strip()]
        if not backend_list:
            return []
        limit_i = max(1, int(limit))

        out: list[dict[str, Any]] = []
        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            # --- backend 候補に一致する queued job を先に読む ---
            rows = db.execute(
                text(
                    """
                    SELECT job_id
                      FROM agent_jobs
                     WHERE status = 'queued'
                       AND backend IN :backends
                     ORDER BY created_at ASC
                     LIMIT :limit
                    """
                ).bindparams(bindparam("backends", expanding=True)),
                {"backends": list(backend_list), "limit": int(limit_i)},
            ).fetchall()
            for row in rows:
                job_id = str(row[0] or "").strip()
                if not job_id:
                    continue
                claim_token = str(uuid.uuid4())
                claimed = db.execute(
                    text(
                        """
                        UPDATE agent_jobs
                           SET status='claimed',
                               claim_token=:claim_token,
                               runner_id=:runner_id,
                               attempts=attempts+1,
                               updated_at=:updated_at
                         WHERE job_id=:job_id
                           AND status='queued'
                        """
                    ),
                    {
                        "claim_token": str(claim_token),
                        "runner_id": str(runner_id_norm),
                        "updated_at": int(now_system_ts),
                        "job_id": str(job_id),
                    },
                )
                if int(claimed.rowcount or 0) != 1:
                    continue
                job = db.query(AgentJob).filter(AgentJob.job_id == str(job_id)).one_or_none()
                if job is None:
                    continue
                out.append(
                    {
                        "job_id": str(job.job_id),
                        "claim_token": str(claim_token),
                        "backend": str(job.backend),
                        "task_instruction": str(job.task_instruction),
                        "intent_id": str(job.intent_id),
                        "decision_id": str(job.decision_id),
                        "created_at": int(job.created_at or 0),
                    }
                )
        return out

    def heartbeat_agent_job(
        self,
        *,
        job_id: str,
        claim_token: str,
        runner_id: str,
        now_system_ts: int,
    ) -> bool:
        """
        claim 済み/実行中 agent_job の heartbeat を更新する。
        """

        # --- 値を正規化 ---
        job_id_norm = str(job_id or "").strip()
        token_norm = str(claim_token or "").strip()
        runner_id_norm = str(runner_id or "").strip()
        if not job_id_norm or not token_norm or not runner_id_norm:
            return False

        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            row = db.execute(
                text(
                    """
                    UPDATE agent_jobs
                       SET status = CASE WHEN status='claimed' THEN 'running' ELSE status END,
                           started_at = COALESCE(started_at, :started_at),
                           heartbeat_at = :heartbeat_at,
                           updated_at = :updated_at
                     WHERE job_id=:job_id
                       AND claim_token=:claim_token
                       AND runner_id=:runner_id
                       AND status IN ('claimed','running')
                    """
                ),
                {
                    "started_at": int(now_system_ts),
                    "heartbeat_at": int(now_system_ts),
                    "updated_at": int(now_system_ts),
                    "job_id": str(job_id_norm),
                    "claim_token": str(token_norm),
                    "runner_id": str(runner_id_norm),
                },
            )
            return int(row.rowcount or 0) == 1

    def list_recent_agent_jobs(
        self,
        *,
        limit: int,
        status: str | None = None,
        backend: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        agent_jobs 一覧（新しい順）を返す。
        """

        limit_i = max(1, int(limit))
        status_norm = str(status or "").strip()
        backend_norm = str(backend or "").strip()
        where_parts: list[str] = []
        params: dict[str, Any] = {"limit": int(limit_i)}
        if status_norm:
            where_parts.append("status = :status")
            params["status"] = str(status_norm)
        if backend_norm:
            where_parts.append("backend = :backend")
            params["backend"] = str(backend_norm)
        where_sql = ""
        if where_parts:
            where_sql = "WHERE " + " AND ".join(where_parts)

        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            rows = db.execute(
                text(
                    f"""
                    SELECT job_id, intent_id, decision_id, backend, task_instruction, status,
                           runner_id, attempts, heartbeat_at, result_status, result_summary_text,
                           error_code, error_message, created_at, started_at, finished_at, updated_at
                      FROM agent_jobs
                      {where_sql}
                     ORDER BY updated_at DESC, created_at DESC
                     LIMIT :limit
                    """
                ),
                params,
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                out.append(
                    {
                        "job_id": str(row[0]),
                        "intent_id": str(row[1]),
                        "decision_id": str(row[2]),
                        "backend": str(row[3]),
                        "task_instruction": str(row[4]),
                        "status": str(row[5]),
                        "runner_id": (str(row[6]) if row[6] is not None else None),
                        "attempts": int(row[7] or 0),
                        "heartbeat_at": (int(row[8]) if row[8] is not None else None),
                        "result_status": (str(row[9]) if row[9] is not None else None),
                        "result_summary_text": (str(row[10]) if row[10] is not None else None),
                        "error_code": (str(row[11]) if row[11] is not None else None),
                        "error_message": (str(row[12]) if row[12] is not None else None),
                        "created_at": int(row[13] or 0),
                        "started_at": (int(row[14]) if row[14] is not None else None),
                        "finished_at": (int(row[15]) if row[15] is not None else None),
                        "updated_at": int(row[16] or 0),
                    }
                )
            return out

    def get_agent_job(self, *, job_id: str) -> dict[str, Any] | None:
        """
        agent_job 1件を返す。
        """

        job_id_norm = str(job_id or "").strip()
        if not job_id_norm:
            return None
        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            row = db.execute(
                text(
                    """
                    SELECT job_id, intent_id, decision_id, backend, task_instruction, status,
                           runner_id, attempts, heartbeat_at, result_status, result_summary_text,
                           error_code, error_message, created_at, started_at, finished_at, updated_at
                      FROM agent_jobs
                     WHERE job_id = :job_id
                    """
                ),
                {"job_id": str(job_id_norm)},
            ).fetchone()
            if row is None:
                return None
            return {
                "job_id": str(row[0]),
                "intent_id": str(row[1]),
                "decision_id": str(row[2]),
                "backend": str(row[3]),
                "task_instruction": str(row[4]),
                "status": str(row[5]),
                "runner_id": (str(row[6]) if row[6] is not None else None),
                "attempts": int(row[7] or 0),
                "heartbeat_at": (int(row[8]) if row[8] is not None else None),
                "result_status": (str(row[9]) if row[9] is not None else None),
                "result_summary_text": (str(row[10]) if row[10] is not None else None),
                "error_code": (str(row[11]) if row[11] is not None else None),
                "error_message": (str(row[12]) if row[12] is not None else None),
                "created_at": int(row[13] or 0),
                "started_at": (int(row[14]) if row[14] is not None else None),
                "finished_at": (int(row[15]) if row[15] is not None else None),
                "updated_at": int(row[16] or 0),
            }

    def recover_runtime_from_latest_snapshot(
        self,
        *,
        now_system_ts: int,
        now_domain_ts: int,
        max_requeue_intents: int = 256,
    ) -> dict[str, Any]:
        """
        最新 runtime snapshot から active intents を復元する。

        方針:
            - snapshot 内の active_intent_ids を読み、再起動で取り残された `running` を `queued` に戻す。
            - `queued` のものも含めて execute_intent ジョブを再投入する（execute 側は queued->running claim で冪等）。
            - `blocked` は外部条件待ちとして維持する（勝手に実行しない）。
        """

        # --- 上限を正規化 ---
        limit_i = max(1, int(max_requeue_intents))

        with memory_session_scope(self.embedding_preset_id, self.embedding_dimension) as db:
            # --- 最新 snapshot を取得 ---
            latest = (
                db.query(RuntimeSnapshot)
                .order_by(RuntimeSnapshot.snapshot_id.desc())
                .first()
            )
            if latest is None:
                return {
                    "restored": False,
                    "snapshot_id": None,
                    "active_intent_ids": [],
                    "active_ids_in_snapshot": 0,
                    "requeued_running_intents": 0,
                    "reenqueued_execute_jobs": 0,
                }

            # --- payload から active_intent_ids を抽出 ---
            payload = common_utils.json_loads_maybe(str(latest.payload_json or ""))
            if not isinstance(payload, dict):
                payload = {}
            active_ids_raw = payload.get("active_intent_ids")
            if not isinstance(active_ids_raw, list):
                active_ids_raw = []

            # --- 重複/空を除去（snapshot 順序を維持） ---
            active_ids: list[str] = []
            seen: set[str] = set()
            for value in active_ids_raw:
                intent_id = str(value or "").strip()
                if not intent_id or intent_id in seen:
                    continue
                seen.add(intent_id)
                active_ids.append(intent_id)
                if len(active_ids) >= int(limit_i):
                    break

            if not active_ids:
                # --- 復元試行の監査を残す（空でも startup_recovered を記録） ---
                db.add(
                    RuntimeSnapshot(
                        snapshot_kind="startup_recovered",
                        payload_json=common_utils.json_dumps(
                            {
                                "source_snapshot_id": int(latest.snapshot_id),
                                "active_intent_ids": [],
                                "requeued_running_intents": 0,
                                "reenqueued_execute_jobs": 0,
                                "recovered_at_system_utc_ts": int(now_system_ts),
                                "recovered_at_domain_utc_ts": int(now_domain_ts),
                            }
                        ),
                        created_at=int(now_system_ts),
                    )
                )
                return {
                    "restored": True,
                    "snapshot_id": int(latest.snapshot_id),
                    "active_intent_ids": [],
                    "active_ids_in_snapshot": 0,
                    "requeued_running_intents": 0,
                    "reenqueued_execute_jobs": 0,
                }

            # --- 対象 intent を読み込む ---
            intents = (
                db.query(Intent)
                .filter(Intent.intent_id.in_(list(active_ids)))
                .all()
            )
            intents_by_id = {str(x.intent_id): x for x in intents}

            requeued_running_intents = 0
            reenqueue_execute_jobs = 0

            # --- active intents を復元（running -> queued、queued は再実行ジョブだけ投入） ---
            for intent_id in active_ids:
                intent = intents_by_id.get(str(intent_id))
                if intent is None:
                    continue

                status = str(intent.status or "").strip()
                if status in {"done", "dropped"}:
                    continue
                if status == "blocked":
                    # --- blocked は外部条件待ちなので、そのまま維持 ---
                    continue
                if status == "running":
                    intent.status = "queued"
                    intent.updated_at = int(now_system_ts)
                    db.add(intent)
                    requeued_running_intents += 1

                # --- queued/running(->queued) は execute_intent を再投入 ---
                if str(intent.status or "").strip() == "queued":
                    db.add(
                        Job(
                            kind="execute_intent",
                            payload_json=common_utils.json_dumps({"intent_id": str(intent.intent_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_system_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_system_ts),
                            updated_at=int(now_system_ts),
                        )
                    )
                    reenqueue_execute_jobs += 1

            # --- 復元結果を snapshot として記録 ---
            db.add(
                RuntimeSnapshot(
                    snapshot_kind="startup_recovered",
                    payload_json=common_utils.json_dumps(
                        {
                            "source_snapshot_id": int(latest.snapshot_id),
                            "active_intent_ids": list(active_ids),
                            "requeued_running_intents": int(requeued_running_intents),
                            "reenqueued_execute_jobs": int(reenqueue_execute_jobs),
                            "recovered_at_system_utc_ts": int(now_system_ts),
                            "recovered_at_domain_utc_ts": int(now_domain_ts),
                        }
                    ),
                    created_at=int(now_system_ts),
                )
            )

            return {
                "restored": True,
                "snapshot_id": int(latest.snapshot_id),
                "active_intent_ids": list(active_ids),
                "active_ids_in_snapshot": int(len(active_ids)),
                "requeued_running_intents": int(requeued_running_intents),
                "reenqueued_execute_jobs": int(reenqueue_execute_jobs),
            }

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
