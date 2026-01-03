"""
デスクトップウォッチ（能動視覚）

設定（settings.db / global_settings）に従い、一定間隔でデスクトップ担当クライアントへ
キャプチャ要求を送って画像を取得し、人格としてコメントを生成して保存・配信する。

実装方針:
- cron無し運用を前提に、サーバ側の定期タスクから tick() を呼び出す。
- ON/OFF の遷移をメモリ上で検出し、ONになったら5秒後に最初の1枚を確認する。
- 起動時にすでにONの場合は「設定間隔が経過してから」初回を実行する（起動直後に覗かない）。
"""

from __future__ import annotations

import logging
import threading
import time

from cocoro_ghost.config import get_config_store
from cocoro_ghost.deps import get_memory_manager


logger = logging.getLogger(__name__)


class DesktopWatchService:
    """
    デスクトップウォッチの状態管理と実行を行うサービス。

    - 設定の変化（enabled/interval/target）を定期的に読み取り、必要なタイミングで実行する。
    - tick() は複数回呼ばれても安全（重複実行は抑制）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._initialized = False
        self._enabled_prev = False
        self._next_run_ts = 0.0

    def tick(self) -> None:
        """現在設定に基づき、必要ならデスクトップウォッチを1回実行する。"""
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
            # NOTE:
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

            # --- 実行 ---
            if not target_client_id:
                logger.warning("desktop_watch enabled but target_client_id is empty; skipping")
                self._next_run_ts = float(now) + float(interval)
                return

            mm = get_memory_manager()
            mm.run_desktop_watch_once(target_client_id=target_client_id)

            # --- 次回予約 ---
            self._next_run_ts = float(now) + float(interval)
        finally:
            with self._lock:
                self._running = False


_desktop_watch_service = DesktopWatchService()


def get_desktop_watch_service() -> DesktopWatchService:
    """デスクトップウォッチサービスのシングルトンを返す。"""
    return _desktop_watch_service
