"""
自律ループのランタイム制御状態。

役割:
- `/api/control/autonomy` が参照/更新する稼働設定を保持する。
- ループ実行の開始/終了時刻と結果を記録する。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _AutonomyRuntimeState:
    """自律ランタイム状態の内部表現。"""

    enabled: bool = True
    periodic_interval_seconds: int = 5
    last_cycle_started_at: int | None = None
    last_cycle_finished_at: int | None = None
    last_cycle_status: str | None = None


_LOCK = threading.Lock()
_STATE = _AutonomyRuntimeState()


def get_runtime_state() -> _AutonomyRuntimeState:
    """現在の自律ランタイム状態を返す。"""

    # --- 参照一貫性のためコピーを返す ---
    with _LOCK:
        return _AutonomyRuntimeState(
            enabled=bool(_STATE.enabled),
            periodic_interval_seconds=int(_STATE.periodic_interval_seconds),
            last_cycle_started_at=(
                int(_STATE.last_cycle_started_at) if _STATE.last_cycle_started_at is not None else None
            ),
            last_cycle_finished_at=(
                int(_STATE.last_cycle_finished_at) if _STATE.last_cycle_finished_at is not None else None
            ),
            last_cycle_status=(str(_STATE.last_cycle_status) if _STATE.last_cycle_status is not None else None),
        )


def update_runtime_control(*, enabled: bool, periodic_interval_seconds: int) -> _AutonomyRuntimeState:
    """有効/無効と周期を更新して新状態を返す。"""

    # --- 入力を正規化 ---
    interval = int(periodic_interval_seconds)
    if interval < 1:
        raise ValueError("periodic_interval_seconds must be >= 1")

    # --- 状態を更新 ---
    with _LOCK:
        _STATE.enabled = bool(enabled)
        _STATE.periodic_interval_seconds = int(interval)
        return _AutonomyRuntimeState(
            enabled=bool(_STATE.enabled),
            periodic_interval_seconds=int(_STATE.periodic_interval_seconds),
            last_cycle_started_at=(
                int(_STATE.last_cycle_started_at) if _STATE.last_cycle_started_at is not None else None
            ),
            last_cycle_finished_at=(
                int(_STATE.last_cycle_finished_at) if _STATE.last_cycle_finished_at is not None else None
            ),
            last_cycle_status=(str(_STATE.last_cycle_status) if _STATE.last_cycle_status is not None else None),
        )


def is_enabled() -> bool:
    """自律ループが有効かを返す。"""

    with _LOCK:
        return bool(_STATE.enabled)


def mark_cycle_started(*, started_at: int) -> None:
    """サイクル開始時刻を記録する。"""

    with _LOCK:
        _STATE.last_cycle_started_at = int(started_at)


def mark_cycle_finished(*, finished_at: int, status: str) -> None:
    """サイクル終了時刻と結果を記録する。"""

    status_s = str(status or "").strip()
    if status_s not in {"succeeded", "failed"}:
        raise ValueError("status must be 'succeeded' or 'failed'")

    with _LOCK:
        _STATE.last_cycle_finished_at = int(finished_at)
        _STATE.last_cycle_status = status_s
