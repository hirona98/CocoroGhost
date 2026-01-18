"""
memory配下の共通ユーティリティ（最小）。

目的:
    - `cocoro_ghost.memory` を分割した後も、共通の小物（時刻など）を1箇所で管理する。
    - 依存を増やしすぎない（必要最小限だけ置く）。
"""

from __future__ import annotations

import time


def now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""
    return int(time.time())

