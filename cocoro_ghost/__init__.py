"""
CocoroGhost パッケージ。

方針:
    - package import 時に重い初期化や再エクスポートを行わない。
    - 主要な責務は下位 package から明示的に参照する。
"""

from __future__ import annotations

__all__ = [
    "api",
    "app_bootstrap",
    "autonomy",
    "core",
    "infra",
    "jobs",
    "llm",
    "memory",
    "reminders",
    "runtime",
    "storage",
    "vision",
]
