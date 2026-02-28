"""
camera_watch policy。

役割:
    - camera_watch の間隔制御を trigger 化する。
    - 実際の観測/実行は Deliberation/Execution 側へ委譲する。
"""

from __future__ import annotations

from typing import Any


def build_camera_watch_trigger(*, now_domain_ts: int, interval_seconds: int) -> dict[str, Any]:
    """
    camera_watch 用の policy trigger 情報を返す。
    """

    # --- 入力を正規化 ---
    now_i = max(0, int(now_domain_ts))
    interval_i = max(1, int(interval_seconds))
    bucket = int(now_i // interval_i)

    # --- trigger 情報 ---
    return {
        "trigger_type": "policy",
        "trigger_key": f"camera_watch:{int(bucket)}",
        "scheduled_at": int(now_i),
        "payload": {
            "policy": "camera_watch",
            "camera_watch_bucket": int(bucket),
            "interval_seconds": int(interval_i),
        },
    }

