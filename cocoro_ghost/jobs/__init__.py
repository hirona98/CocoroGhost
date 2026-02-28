"""
ジョブ実行配線パッケージ。

目的:
    - jobs.kind から実処理への配線を package として分離する。
    - Worker 本体は registry のみを参照する。
"""

from __future__ import annotations

from cocoro_ghost.jobs.registry import run_job_kind

__all__ = ["run_job_kind"]
