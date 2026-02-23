"""
Worker共通定数

役割:
- worker.py（スケジューラ）と worker_handlers.py（実処理）で共有する定数を一元管理する
"""

from __future__ import annotations


# --- Jobステータス ---
# NOTE:
# - memory_models.Job.status の値と対応させる。
JOB_PENDING = 0
JOB_RUNNING = 1
JOB_DONE = 2
JOB_FAILED = 3


# --- ジョブ回収 ---
JOB_RETRY_BASE_SECONDS = 5
JOB_RETRY_MAX_SECONDS = 300
JOB_RUNNING_STALE_SECONDS = 120
JOB_STALE_SWEEP_INTERVAL_SECONDS = 10


# --- 汎用エージェント委譲ジョブ回収 ---
# NOTE:
# - agent_runner は長時間処理を想定するため、worker の通常ジョブより長めにする。
AGENT_JOB_STALE_SECONDS = 300


# --- 記憶整理（tidy_memory） ---
TIDY_CHAT_TURNS_INTERVAL = 200
TIDY_CHAT_TURNS_INTERVAL_FIRST = 10
TIDY_MAX_CLOSE_PER_RUN = 200
TIDY_ACTIVE_STATE_FETCH_LIMIT = 5000
