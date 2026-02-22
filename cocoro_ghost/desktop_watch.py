"""
デスクトップウォッチ（能動視覚）

設定（settings.db / global_settings）に従い、一定間隔で desktop_watch policy trigger を投入する。

実装方針:
- cron無し運用を前提に、サーバ側の定期タスクから tick() を呼び出す。
- ON/OFF の遷移をメモリ上で検出し、ONになったら5秒後に最初の1枚を確認する。
- 起動時にすでにONの場合は「設定間隔が経過してから」初回を実行する（起動直後に覗かない）。
- 実際の観測実行は Deliberation/Execution（vision_perception capability）へ委譲する。
"""

from __future__ import annotations

import logging
import threading
import time

from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.autonomy.policies import build_desktop_watch_trigger
from cocoro_ghost.clock import get_clock_service
from cocoro_ghost.config import get_config_store


logger = logging.getLogger(__name__)


class DesktopWatchService:
    """
    デスクトップウォッチの状態管理と policy trigger 投入を行うサービス。

    - 設定の変化（enabled/interval/target）を定期的に読み取り、必要なタイミングで trigger を投入する。
    - tick() は複数回呼ばれても安全（重複実行は抑制）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._initialized = False
        self._enabled_prev = False
        self._next_run_ts = 0.0

    def tick(self) -> None:
        """現在設定に基づき、必要なら desktop_watch policy trigger を1件投入する。"""
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            cfg = get_config_store().config
            enabled = bool(cfg.desktop_watch_enabled)
            interval = max(1, int(cfg.desktop_watch_interval_seconds))
            target_client_id = (cfg.desktop_watch_target_client_id or "").strip()

            now = time.time()

            # --- 初回tick（起動直後） ---
            # - 起動時に desktop_watch_enabled がすでに True の場合、
            #   「5秒後に即覗く」ではなく「設定間隔が経過してから」初回実行する。
            # - UIでのON操作（OFF→ON遷移）とは挙動を分ける。
            if not self._initialized:
                self._initialized = True
                if enabled:
                    self._enabled_prev = True
                    self._next_run_ts = float(now) + float(interval)
                    logger.info(
                        "desktop_watch enabled at startup; first capture scheduled in_seconds=%s",
                        int(interval),
                    )
                else:
                    self._enabled_prev = False
                    self._next_run_ts = 0.0
                return

            # --- OFF時は状態をリセット ---
            if not enabled:
                self._enabled_prev = False
                self._next_run_ts = 0.0
                return

            # --- ONへの遷移: 5秒後に初回確認 ---
            if not self._enabled_prev:
                self._enabled_prev = True
                self._next_run_ts = float(now) + 5.0
                logger.info("desktop_watch enabled; first capture scheduled in_seconds=%s", 5)
                return

            # --- 実行タイミング待ち ---
            if float(now) < float(self._next_run_ts):
                return

            # --- policy trigger 投入 ---
            if not target_client_id:
                logger.warning("desktop_watch enabled but target_client_id is empty; skipping")
                self._next_run_ts = float(now) + float(interval)
                return

            # --- domain時刻で trigger を組み立てる ---
            now_domain_ts = int(get_clock_service().now_domain_utc_ts())
            trigger = build_desktop_watch_trigger(
                now_domain_ts=int(now_domain_ts),
                interval_seconds=int(interval),
                target_client_id=str(target_client_id),
            )
            inserted = get_autonomy_orchestrator().enqueue_policy_trigger(
                trigger_key=str(trigger["trigger_key"]),
                payload=dict(trigger.get("payload") or {}),
                scheduled_at=int(trigger["scheduled_at"]),
            )
            if not bool(inserted):
                logger.debug("desktop_watch trigger deduped key=%s", str(trigger["trigger_key"]))

            # --- 次回予約 ---
            self._next_run_ts = float(now) + float(interval)
        finally:
            with self._lock:
                self._running = False


_desktop_watch_service = DesktopWatchService()


def get_desktop_watch_service() -> DesktopWatchService:
    """デスクトップウォッチサービスのシングルトンを返す。"""
    return _desktop_watch_service
