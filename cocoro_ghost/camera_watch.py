"""
カメラウォッチ（Policy trigger 生成）

役割:
    - settings の camera_watch_* に従い、一定間隔で autonomy の policy trigger を投入する。
    - ここでは観測実行を行わない（Perception/Execution は autonomy 側の責務）。
"""

from __future__ import annotations

import logging
import threading
import time

from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.autonomy.policies import build_camera_watch_trigger
from cocoro_ghost.clock import get_clock_service
from cocoro_ghost.config import get_config_store


logger = logging.getLogger(__name__)


class CameraWatchService:
    """
    camera_watch の間隔制御と trigger 投入を行うサービス。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._initialized = False
        self._enabled_prev = False
        self._next_run_monotonic_ts = 0.0

    def tick(self) -> None:
        """現在設定に基づき、必要なら camera_watch policy trigger を1件投入する。"""

        # --- 多重実行を抑止 ---
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            cfg = get_config_store().config
            enabled = bool(getattr(cfg, "camera_watch_enabled", False))
            interval = max(1, int(getattr(cfg, "camera_watch_interval_seconds", 15)))
            now_monotonic = float(time.monotonic())

            # --- 初回tick（起動直後） ---
            if not self._initialized:
                self._initialized = True
                self._enabled_prev = bool(enabled)
                if enabled:
                    self._next_run_monotonic_ts = float(now_monotonic) + float(interval)
                return

            # --- OFF時は状態をリセット ---
            if not enabled:
                self._enabled_prev = False
                self._next_run_monotonic_ts = 0.0
                return

            # --- OFF→ON 遷移 ---
            if not self._enabled_prev:
                self._enabled_prev = True
                self._next_run_monotonic_ts = float(now_monotonic) + 2.0
                return

            # --- 実行時刻待ち ---
            if float(now_monotonic) < float(self._next_run_monotonic_ts):
                return

            # --- policy trigger を投入 ---
            now_domain_ts = int(get_clock_service().now_domain_utc_ts())
            trigger = build_camera_watch_trigger(
                now_domain_ts=int(now_domain_ts),
                interval_seconds=int(interval),
            )
            inserted = get_autonomy_orchestrator().enqueue_policy_trigger(
                trigger_key=str(trigger["trigger_key"]),
                payload=dict(trigger.get("payload") or {}),
                scheduled_at=int(trigger["scheduled_at"]),
            )
            if not bool(inserted):
                logger.debug("camera_watch trigger deduped key=%s", str(trigger["trigger_key"]))

            # --- 次回予約 ---
            self._next_run_monotonic_ts = float(now_monotonic) + float(interval)
        finally:
            with self._lock:
                self._running = False


_camera_watch_service = CameraWatchService()


def get_camera_watch_service() -> CameraWatchService:
    """CameraWatchService のシングルトンを返す。"""

    return _camera_watch_service

