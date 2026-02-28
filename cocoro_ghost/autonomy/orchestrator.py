"""
autonomy オーケストレータ。

役割:
    - heartbeat と due trigger を管理する。
    - due trigger を Worker ジョブ（deliberate_once）へ橋渡しする。
"""

from __future__ import annotations

import json
import hashlib
import logging
import threading
from typing import Any

from cocoro_ghost.autonomy.repository import AutonomyRepository
from cocoro_ghost.autonomy.runtime_blackboard import get_runtime_blackboard
from cocoro_ghost.clock import get_clock_service
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import memory_session_scope, settings_session_scope
from cocoro_ghost.memory_models import AgendaThread
from cocoro_ghost.models import GlobalSettings


logger = logging.getLogger(__name__)


class AutonomyOrchestrator:
    """自発行動の trigger/job 制御を担当する。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._last_heartbeat_trigger_key: str | None = None
        self._last_snapshot_bucket: int | None = None
        self._runtime_recovered_once = False

    def tick(self) -> None:
        """
        1tick 処理を実行する（heartbeat投入 + due trigger dispatch）。
        """

        # --- 多重実行を抑止 ---
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            settings = self._load_runtime_settings()
            if settings is None:
                return
            if not bool(settings["autonomy_enabled"]):
                return

            # --- 時刻とリポジトリ ---
            clock = get_clock_service()
            now_domain_ts = int(clock.now_domain_utc_ts())
            now_system_ts = int(clock.now_system_utc_ts())
            cfg = get_config_store().config
            repo = AutonomyRepository(
                embedding_preset_id=str(cfg.embedding_preset_id),
                embedding_dimension=int(cfg.embedding_dimension),
            )

            # --- 起動後1回だけ runtime snapshot を復元 ---
            if not bool(self._runtime_recovered_once):
                recovered = repo.recover_runtime_from_latest_snapshot(
                    now_system_ts=int(now_system_ts),
                    now_domain_ts=int(now_domain_ts),
                    max_requeue_intents=256,
                )
                self._runtime_recovered_once = True
                get_runtime_blackboard().mark_recovered(
                    source_snapshot_id=(
                        int(recovered.get("snapshot_id"))
                        if recovered.get("snapshot_id") is not None
                        else None
                    ),
                    active_intent_ids=[str(x) for x in list(recovered.get("active_intent_ids") or [])],
                    now_system_ts=int(now_system_ts),
                    now_domain_ts=int(now_domain_ts),
                )
                logger.info(
                    "autonomy runtime snapshot recovered restored=%s snapshot_id=%s active=%s requeued_running=%s reenqueue_jobs=%s",
                    bool(recovered.get("restored")),
                    recovered.get("snapshot_id"),
                    int(recovered.get("active_ids_in_snapshot") or 0),
                    int(recovered.get("requeued_running_intents") or 0),
                    int(recovered.get("reenqueued_execute_jobs") or 0),
                )

            # --- heartbeat trigger は due な agenda_threads がある時だけ投入する ---
            heartbeat_seconds = int(settings["autonomy_heartbeat_seconds"])
            due_agenda_threads = self._list_due_agenda_threads(
                embedding_preset_id=str(cfg.embedding_preset_id),
                embedding_dimension=int(cfg.embedding_dimension),
                now_domain_ts=int(now_domain_ts),
            )
            if due_agenda_threads:
                heartbeat_bucket = int(now_domain_ts // max(1, int(heartbeat_seconds)))
                signature_src = "|".join(str(x.get("thread_id") or "") for x in list(due_agenda_threads or []))
                signature_hash = hashlib.sha1(signature_src.encode("utf-8")).hexdigest()[:12]
                heartbeat_trigger_key = f"heartbeat:{int(heartbeat_bucket)}:{signature_hash}"
                if self._last_heartbeat_trigger_key != str(heartbeat_trigger_key):
                    self._last_heartbeat_trigger_key = str(heartbeat_trigger_key)
                    heartbeat_payload: dict[str, Any] = {
                        "heartbeat_bucket": int(heartbeat_bucket),
                        "heartbeat_reason": "agenda_due",
                        "due_agenda_thread_ids": [
                            str(x.get("thread_id") or "")
                            for x in list(due_agenda_threads or [])
                            if str(x.get("thread_id") or "").strip()
                        ],
                        "due_agenda_count": int(len(due_agenda_threads)),
                    }
                    top_due = dict(due_agenda_threads[0])
                    top_action_type = str(top_due.get("next_action_type") or "").strip()
                    top_action_payload = (
                        dict(top_due.get("next_action_payload") or {})
                        if isinstance(top_due.get("next_action_payload"), dict)
                        else {}
                    )
                    if top_action_type:
                        heartbeat_payload["suggested_action_type"] = str(top_action_type)
                        heartbeat_payload["suggested_action_payload"] = dict(top_action_payload)
                        heartbeat_payload["agenda_thread_id"] = str(top_due.get("thread_id") or "")
                        heartbeat_payload["agenda_topic"] = str(top_due.get("topic") or "")
                    repo.enqueue_trigger(
                        trigger_type="heartbeat",
                        trigger_key=str(heartbeat_trigger_key),
                        payload=heartbeat_payload,
                        now_system_ts=int(now_system_ts),
                        scheduled_at=int(now_domain_ts),
                        source_event_id=None,
                    )
            else:
                self._last_heartbeat_trigger_key = None


            # --- runtime snapshot ジョブ（30秒ごと）を投入 ---
            snapshot_interval_seconds = 30
            snapshot_bucket = int(now_system_ts // int(snapshot_interval_seconds))
            if self._last_snapshot_bucket != int(snapshot_bucket):
                self._last_snapshot_bucket = int(snapshot_bucket)
                repo.enqueue_job(
                    kind="snapshot_runtime",
                    payload={
                        "snapshot_kind": "periodic",
                        "snapshot_bucket": int(snapshot_bucket),
                    },
                    run_after_system_ts=int(now_system_ts),
                    now_system_ts=int(now_system_ts),
                )

            # --- due trigger を claimed にして deliberate_once を投入 ---
            claim_limit = max(1, int(settings["autonomy_max_parallel_intents"]) * 2)
            claimed = repo.claim_due_triggers(
                now_domain_ts=int(now_domain_ts),
                now_system_ts=int(now_system_ts),
                limit=int(claim_limit),
            )
            for trigger in claimed:
                repo.enqueue_job(
                    kind="deliberate_once",
                    payload={
                        "trigger_id": str(trigger.trigger_id),
                        "claim_token": str(trigger.claim_token),
                    },
                    run_after_system_ts=int(now_system_ts),
                    now_system_ts=int(now_system_ts),
                )
        finally:
            with self._lock:
                self._running = False

    def enqueue_event_trigger(
        self,
        *,
        event_id: int,
        source: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """
        event 起点の trigger を queued 追加する。
        """

        # --- 設定がOFFなら積まない ---
        settings = self._load_runtime_settings()
        if settings is None:
            return False
        if not bool(settings["autonomy_enabled"]):
            return False

        # --- trigger を追加 ---
        clock = get_clock_service()
        now_domain_ts = int(clock.now_domain_utc_ts())
        now_system_ts = int(clock.now_system_utc_ts())
        cfg = get_config_store().config
        repo = AutonomyRepository(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
        )
        trigger_payload = dict(payload or {})
        trigger_payload["source"] = str(source)
        return repo.enqueue_trigger(
            trigger_type="event",
            trigger_key=f"event:{int(event_id)}",
            payload=trigger_payload,
            now_system_ts=int(now_system_ts),
            scheduled_at=int(now_domain_ts),
            source_event_id=int(event_id),
        )

    def enqueue_manual_trigger(
        self,
        *,
        trigger_type: str,
        trigger_key: str,
        payload: dict[str, Any] | None,
        scheduled_at: int | None,
    ) -> bool:
        """
        手動トリガを queued 追加する（制御API用）。
        """

        # --- 時刻とリポジトリ ---
        clock = get_clock_service()
        now_domain_ts = int(clock.now_domain_utc_ts())
        now_system_ts = int(clock.now_system_utc_ts())
        cfg = get_config_store().config
        repo = AutonomyRepository(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
        )
        scheduled = int(scheduled_at) if scheduled_at is not None else int(now_domain_ts)
        return repo.enqueue_trigger(
            trigger_type=str(trigger_type),
            trigger_key=str(trigger_key),
            payload=dict(payload or {}),
            now_system_ts=int(now_system_ts),
            scheduled_at=int(scheduled),
            source_event_id=None,
        )

    def enqueue_time_trigger(
        self,
        *,
        trigger_key: str,
        payload: dict[str, Any] | None,
        scheduled_at: int | None,
    ) -> bool:
        """
        time 起点の trigger を queued 追加する（Policy用）。
        """

        # --- 設定がOFFなら積まない ---
        settings = self._load_runtime_settings()
        if settings is None:
            return False
        if not bool(settings["autonomy_enabled"]):
            return False

        # --- 時刻とリポジトリ ---
        clock = get_clock_service()
        now_domain_ts = int(clock.now_domain_utc_ts())
        now_system_ts = int(clock.now_system_utc_ts())
        cfg = get_config_store().config
        repo = AutonomyRepository(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
        )
        scheduled = int(scheduled_at) if scheduled_at is not None else int(now_domain_ts)
        return repo.enqueue_trigger(
            trigger_type="time",
            trigger_key=str(trigger_key),
            payload=dict(payload or {}),
            now_system_ts=int(now_system_ts),
            scheduled_at=int(scheduled),
            source_event_id=None,
        )

    def enqueue_policy_trigger(
        self,
        *,
        trigger_key: str,
        payload: dict[str, Any] | None,
        scheduled_at: int | None,
    ) -> bool:
        """
        policy 起点の trigger を queued 追加する（Policy用）。
        """

        # --- 設定がOFFなら積まない ---
        settings = self._load_runtime_settings()
        if settings is None:
            return False
        if not bool(settings["autonomy_enabled"]):
            return False

        # --- 時刻とリポジトリ ---
        clock = get_clock_service()
        now_domain_ts = int(clock.now_domain_utc_ts())
        now_system_ts = int(clock.now_system_utc_ts())
        cfg = get_config_store().config
        repo = AutonomyRepository(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
        )
        scheduled = int(scheduled_at) if scheduled_at is not None else int(now_domain_ts)
        return repo.enqueue_trigger(
            trigger_type="policy",
            trigger_key=str(trigger_key),
            payload=dict(payload or {}),
            now_system_ts=int(now_system_ts),
            scheduled_at=int(scheduled),
            source_event_id=None,
        )

    def get_status(self) -> dict[str, Any]:
        """
        autonomy の稼働状態を返す（制御API用）。
        """

        # --- 設定と時刻 ---
        settings = self._load_runtime_settings() or {
            "autonomy_enabled": False,
            "autonomy_heartbeat_seconds": 30,
            "autonomy_max_parallel_intents": 2,
        }
        now_domain_ts = int(get_clock_service().now_domain_utc_ts())
        cfg = get_config_store().config
        repo = AutonomyRepository(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
        )
        counts = repo.get_status_counts(now_domain_ts=int(now_domain_ts))
        return {
            "autonomy_enabled": bool(settings["autonomy_enabled"]),
            "autonomy_heartbeat_seconds": int(settings["autonomy_heartbeat_seconds"]),
            "autonomy_max_parallel_intents": int(settings["autonomy_max_parallel_intents"]),
            "now_domain_utc_ts": int(now_domain_ts),
            **counts,
        }

    def list_intents(self, *, limit: int) -> list[dict[str, Any]]:
        """
        intent 一覧を返す（制御API用）。
        """

        cfg = get_config_store().config
        repo = AutonomyRepository(
            embedding_preset_id=str(cfg.embedding_preset_id),
            embedding_dimension=int(cfg.embedding_dimension),
        )
        return repo.list_recent_intents(limit=int(limit))

    def _load_runtime_settings(self) -> dict[str, Any] | None:
        """
        settings.db から autonomy 設定を読む。
        """

        # --- GlobalSettings を1行読む ---
        with settings_session_scope() as session:
            row = session.query(GlobalSettings).first()
            if row is None:
                return None
            return {
                "autonomy_enabled": bool(getattr(row, "autonomy_enabled", False)),
                "autonomy_heartbeat_seconds": max(1, int(getattr(row, "autonomy_heartbeat_seconds", 30))),
                "autonomy_max_parallel_intents": max(1, int(getattr(row, "autonomy_max_parallel_intents", 2))),
            }

    def _list_due_agenda_threads(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        now_domain_ts: int,
    ) -> list[dict[str, Any]]:
        """
        次の一手が due になっている agenda_threads を返す。
        """

        # --- active/open/blocked の中から、次アクションがある due thread だけを見る。 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rows = (
                db.query(AgendaThread)
                .filter(AgendaThread.status.in_(["active", "open", "blocked"]))
                .filter(AgendaThread.followup_due_at.isnot(None))
                .filter(AgendaThread.followup_due_at <= int(now_domain_ts))
                .filter(AgendaThread.next_action_type.isnot(None))
                .order_by(AgendaThread.updated_at.desc())
                .limit(16)
                .all()
            )

        # --- active を優先しつつ、priority と due 時刻で安定並びにする。 ---
        status_rank = {
            "active": 0,
            "open": 1,
            "blocked": 2,
        }
        ordered_rows = sorted(
            list(rows or []),
            key=lambda row: (
                int(status_rank.get(str(row.status or ""), 9)),
                -int(row.priority or 0),
                int(row.followup_due_at or 0),
                -int(row.updated_at or 0),
                str(row.thread_id or ""),
            ),
        )[:8]

        out: list[dict[str, Any]] = []
        for row in list(ordered_rows or []):
            if not str(row.next_action_type or "").strip():
                continue
            payload_obj = json.loads(str(row.next_action_payload_json or "{}"))
            if not isinstance(payload_obj, dict):
                raise RuntimeError("agenda_threads.next_action_payload_json is not an object")
            out.append(
                {
                    "thread_id": str(row.thread_id),
                    "topic": str(row.topic),
                    "next_action_type": str(row.next_action_type),
                    "next_action_payload": dict(payload_obj),
                }
            )
        return out


_orchestrator = AutonomyOrchestrator()


def get_autonomy_orchestrator() -> AutonomyOrchestrator:
    """AutonomyOrchestrator シングルトンを返す。"""

    return _orchestrator
