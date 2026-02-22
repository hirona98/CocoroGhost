"""
device_control capability。

役割:
    - 将来の家電制御 action を受けるための明示的な入口。
    - 現時点ではデバイス統合が未接続のため、構造化失敗を返す。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost import common_utils
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult


def execute_device_control(*, action_payload: dict[str, Any]) -> CapabilityExecutionResult:
    """
    device_action を実行する。
    """

    # --- 入力を監査用に保持 ---
    payload = dict(action_payload or {})

    # --- 現時点の実装状態を明示的に返す ---
    return CapabilityExecutionResult(
        result_status="failed",
        summary="device_action は未接続です（device_control integration が未実装）。",
        result_payload_json=common_utils.json_dumps({"action_payload": payload}),
        useful_for_recall_hint=0,
        next_trigger=None,
    )

