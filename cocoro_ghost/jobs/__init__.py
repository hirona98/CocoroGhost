"""
ジョブ実行配線パッケージ。

目的:
    - jobs.kind から実処理への配線と runner を package として分離する。
    - 非同期ジョブの入口を `jobs` 配下で辿れるようにする。
"""

from __future__ import annotations

from cocoro_ghost.jobs.registry import run_job_kind
from cocoro_ghost.jobs.runner import run_forever

__all__ = ["run_forever", "run_job_kind"]
