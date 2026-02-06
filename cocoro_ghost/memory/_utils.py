"""
memory配下の共通ユーティリティ（最小）。

目的:
    - `cocoro_ghost.memory` を分割した後も、共通の小物（時刻など）を1箇所で管理する。
    - 依存を増やしすぎない（必要最小限だけ置く）。
"""

from __future__ import annotations

from cocoro_ghost.clock import get_clock_service


def now_utc_ts() -> int:
    """
    domain時刻（UTC）をUNIX秒で返す。

    目的:
        - 会話/記憶/感情のドメイン時刻を、実時間から切り離して扱う。
    """
    return int(get_clock_service().now_domain_utc_ts())


def now_system_utc_ts() -> int:
    """
    system時刻（UTC）をUNIX秒で返す。

    目的:
        - ジョブスケジューリングなど、OS実時間基準が必要な箇所で使う。
    """
    return int(get_clock_service().now_system_utc_ts())
