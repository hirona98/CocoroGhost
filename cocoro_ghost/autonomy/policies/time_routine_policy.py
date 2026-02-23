"""
time_routine policy。

役割:
    - 設定間隔（秒）単位の時刻ルーチン判定を policy trigger 化する。
    - 実際の行動選択は Deliberation に委譲する。
"""

from __future__ import annotations

from typing import Any


def build_time_routine_trigger(*, now_domain_ts: int, interval_seconds: int) -> dict[str, Any]:
    """
    time_routine 用の policy trigger 情報を返す。
    """

    # --- 設定秒数バケットで重複排除しやすくする ---
    now_i = max(0, int(now_domain_ts))
    interval_i = max(1, int(interval_seconds))
    time_bucket = int(now_i // int(interval_i))

    # --- trigger 情報 ---
    return {
        "trigger_type": "policy",
        "trigger_key": f"time_routine:{int(time_bucket)}",
        "scheduled_at": int(now_i),
        "payload": {
            "policy": "time_routine",
            "time_bucket": int(time_bucket),
            "interval_seconds": int(interval_i),
        },
    }
