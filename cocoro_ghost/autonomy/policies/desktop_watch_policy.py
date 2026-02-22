"""
desktop_watch policy。

役割:
    - desktop_watch の間隔制御を trigger 化する。
    - 実際の観測/実行は Deliberation/Execution 側へ委譲する。
"""

from __future__ import annotations

from typing import Any


def build_desktop_watch_trigger(
    *,
    now_domain_ts: int,
    interval_seconds: int,
    target_client_id: str,
) -> dict[str, Any]:
    """
    desktop_watch 用の policy trigger 情報を返す。
    """

    # --- 入力を正規化 ---
    now_i = max(0, int(now_domain_ts))
    interval_i = max(1, int(interval_seconds))
    target_client_id_norm = str(target_client_id or "").strip()
    bucket = int(now_i // interval_i)

    # --- trigger 情報 ---
    return {
        "trigger_type": "policy",
        "trigger_key": f"desktop_watch:{target_client_id_norm}:{int(bucket)}",
        "scheduled_at": int(now_i),
        "payload": {
            "policy": "desktop_watch",
            "desktop_watch_bucket": int(bucket),
            "interval_seconds": int(interval_i),
            "target_client_id": str(target_client_id_norm),
            "suggested_action_type": "observe_screen",
            "suggested_action_payload": {
                "target_client_id": str(target_client_id_norm),
            },
        },
    }

