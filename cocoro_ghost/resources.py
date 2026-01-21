"""
配布/開発共通のリソースパス解決。

目的:
    - Web UI の静的ファイル（static/）を FastAPI から配信する。
    - PyInstaller（frozen）では `sys._MEIPASS` 配下に配置されるため、そこも参照する。
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_static_dir() -> Path:
    """static/ ディレクトリのパスを返す。"""

    # --- PyInstaller (frozen) の場合は _MEIPASS を優先 ---
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return (Path(getattr(sys, "_MEIPASS")) / "static").resolve()

    # --- 通常実行は app_root/static を使う ---
    from cocoro_ghost.paths import get_app_root_dir

    return (get_app_root_dir() / "static").resolve()

