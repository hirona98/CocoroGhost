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


# --- ジョブ再試行/回収 ---
JOB_MAX_RETRIES = 2
JOB_RETRY_BASE_SECONDS = 5
JOB_RETRY_MAX_SECONDS = 300
JOB_RUNNING_STALE_SECONDS = 120
JOB_STALE_SWEEP_INTERVAL_SECONDS = 10


# --- 記憶整理（tidy_memory） ---
TIDY_CHAT_TURNS_INTERVAL = 200
TIDY_CHAT_TURNS_INTERVAL_FIRST = 10
TIDY_MAX_CLOSE_PER_RUN = 200
TIDY_ACTIVE_STATE_FETCH_LIMIT = 5000
