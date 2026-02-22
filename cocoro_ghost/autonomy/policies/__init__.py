"""
autonomy policy 実装。

役割:
    - 観測/検討を「いつ起動するか」の判定だけを担当する。
    - LLM呼び出しや Capability 実行は持たない。
"""

from cocoro_ghost.autonomy.policies.camera_watch_policy import build_camera_watch_trigger
from cocoro_ghost.autonomy.policies.desktop_watch_policy import build_desktop_watch_trigger
from cocoro_ghost.autonomy.policies.time_routine_policy import build_time_routine_trigger

__all__ = [
    "build_camera_watch_trigger",
    "build_desktop_watch_trigger",
    "build_time_routine_trigger",
]
