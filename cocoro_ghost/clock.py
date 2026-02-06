"""
アプリ内時計サービス。

目的:
    - 実時間（system）と、検証用に進められる論理時間（domain）を分離する。
    - 感情減衰/記憶時刻などのドメイン挙動を、実時間待ちなしで検証できるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class ClockSnapshot:
    """時計状態の読み取り専用スナップショット。"""

    system_now_utc_ts: int
    domain_now_utc_ts: int
    domain_offset_seconds: int


class ClockService:
    """
    アプリ内で共有する時計サービス。

    方針:
        - system時刻: OSの現在時刻（time.time）をそのまま使う。
        - domain時刻: system時刻 + offset秒。
        - offsetはAPIから進められる（テスト用途）。
    """

    def __init__(self) -> None:
        # --- 可変状態（domainのオフセット秒） ---
        self._lock = threading.Lock()
        self._domain_offset_seconds = 0

    def now_system_utc_ts(self) -> int:
        """system時刻（UTC epoch seconds）を返す。"""

        return int(time.time())

    def now_domain_utc_ts(self) -> int:
        """domain時刻（UTC epoch seconds）を返す。"""

        # --- offsetを読み取り、system時刻へ加算 ---
        with self._lock:
            offset = int(self._domain_offset_seconds)
        return int(time.time()) + int(offset)

    def get_domain_offset_seconds(self) -> int:
        """domain時刻オフセット秒を返す。"""

        with self._lock:
            return int(self._domain_offset_seconds)

    def advance_domain_seconds(self, *, seconds: int) -> int:
        """
        domain時刻を前進させる。

        Returns:
            変更後のoffset秒。
        """

        delta = int(seconds)
        if delta <= 0:
            raise ValueError("seconds must be >= 1")

        # --- 単純加算（単一ユーザー前提） ---
        with self._lock:
            self._domain_offset_seconds = int(self._domain_offset_seconds) + int(delta)
            return int(self._domain_offset_seconds)

    def reset_domain_offset(self) -> None:
        """domain時刻オフセットを0へ戻す。"""

        with self._lock:
            self._domain_offset_seconds = 0

    def snapshot(self) -> ClockSnapshot:
        """時計状態のスナップショットを返す。"""

        # --- system/domainを同一時点で計算 ---
        system_now = int(time.time())
        with self._lock:
            offset = int(self._domain_offset_seconds)
        return ClockSnapshot(
            system_now_utc_ts=int(system_now),
            domain_now_utc_ts=int(system_now) + int(offset),
            domain_offset_seconds=int(offset),
        )


_clock_service = ClockService()


def get_clock_service() -> ClockService:
    """時計サービスのシングルトンを返す。"""

    return _clock_service

