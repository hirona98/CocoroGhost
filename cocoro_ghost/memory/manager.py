"""
memoryパッケージのエントリ（MemoryManager）。

方針:
    - `MemoryManager` 自体は「API層から呼ばれる窓口」として残す。
    - 実装は mixin に分割し、責務（chat/外部入力/画像/ジョブ）を分離する。
"""

from __future__ import annotations

from cocoro_ghost.config import ConfigStore
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.memory._chat_mixin import _ChatMemoryMixin
from cocoro_ghost.memory._external_mixin import _ExternalMemoryMixin
from cocoro_ghost.memory._image_mixin import _ImageMemoryMixin
from cocoro_ghost.memory._jobs_mixin import _JobsMemoryMixin


class MemoryManager(
    _ChatMemoryMixin,
    _ExternalMemoryMixin,
    _ImageMemoryMixin,
    _JobsMemoryMixin,
):
    """記憶操作の窓口（API層から呼ばれる）。"""

    def __init__(self, *, llm_client: LlmClient, config_store: ConfigStore) -> None:
        # --- 依存を保持（mixin側から参照される） ---
        self.llm_client = llm_client
        self.config_store = config_store

