"""
autonomy capability 実装。

役割:
    - Deliberation で決まった action_type を実行する。
    - 意思決定は持たず、ActionResult を返す。
"""

from cocoro_ghost.autonomy.capabilities.device_control import execute_device_control
from cocoro_ghost.autonomy.capabilities.mobility_move import execute_mobility_move
from cocoro_ghost.autonomy.capabilities.schedule_alarm import execute_schedule_alarm
from cocoro_ghost.autonomy.capabilities.vision_perception import execute_vision_perception
from cocoro_ghost.autonomy.capabilities.web_access import execute_web_research

__all__ = [
    "execute_device_control",
    "execute_mobility_move",
    "execute_schedule_alarm",
    "execute_vision_perception",
    "execute_web_research",
]
