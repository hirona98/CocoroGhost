"""
バックグラウンドWorker

役割:
- memory_*.db の jobs をポーリングし、非同期処理を実行する

現時点の最小実装:
- 出来事ログ（events）の埋め込みを生成し、sqlite-vec（vec_items）へ書き込む
  - これにより「非同期更新→次ターンで効く」を最短で満たす

拡張:
- WritePlan を生成し、状態/感情/文脈グラフを更新する
- 状態/感情もベクトル索引へ反映する（次ターンで効く）
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
import time
from typing import Any

from sqlalchemy import func

from cocoro_ghost import common_utils
from cocoro_ghost.storage.db import memory_session_scope
from cocoro_ghost.llm.client import LlmClient
from cocoro_ghost.jobs.constants import (
    JOB_DONE as _JOB_DONE,
    JOB_FAILED as _JOB_FAILED,
    JOB_PENDING as _JOB_PENDING,
    JOB_RUNNING as _JOB_RUNNING,
    JOB_RUNNING_STALE_SECONDS as _JOB_RUNNING_STALE_SECONDS,
    JOB_STALE_SWEEP_INTERVAL_SECONDS as _JOB_STALE_SWEEP_INTERVAL_SECONDS,
)
from cocoro_ghost.storage.memory_models import Job
from cocoro_ghost.jobs import run_job_kind


logger = logging.getLogger(__name__)


def _now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""
    return int(time.time())


def get_job_queue_stats(*, embedding_preset_id: str, embedding_dimension: int) -> dict[str, int]:
    """
    jobs テーブルのランタイム統計を返す。

    目的:
        - queue滞留（pending/due）と実行詰まり（running/stale）をAPIから観測できるようにする。
    """

    # --- 判定時刻を固定する ---
    now_ts = _now_utc_ts()
    stale_before_ts = int(now_ts) - int(_JOB_RUNNING_STALE_SECONDS)

    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        # --- ステータス別の件数 ---
        pending_count = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_PENDING))
            .scalar()
        )
        running_count = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_RUNNING))
            .scalar()
        )
        done_count = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_DONE))
            .scalar()
        )
        failed_count = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_FAILED))
            .scalar()
        )

        # --- 即時実行可能な pending（due） ---
        due_pending_count = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_PENDING))
            .filter(Job.run_after <= int(now_ts))
            .scalar()
        )

        # --- stale running（回収対象） ---
        stale_running_count = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_RUNNING))
            .filter(Job.updated_at <= int(stale_before_ts))
            .scalar()
        )

    # --- API応答向けに整形する ---
    return {
        "pending_count": int(pending_count or 0),
        "due_pending_count": int(due_pending_count or 0),
        "running_count": int(running_count or 0),
        "stale_running_count": int(stale_running_count or 0),
        "done_count": int(done_count or 0),
        "failed_count": int(failed_count or 0),
        "stale_seconds": int(_JOB_RUNNING_STALE_SECONDS),
    }


def run_forever(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    poll_interval_seconds: float,
    max_jobs_per_tick: int,
    periodic_interval_seconds: float,
    job_concurrency: int,
    stop_event,
) -> None:
    """
    jobs を監視して実行し続ける（内蔵Worker用エントリポイント）。

    - due(pending & run_after<=now) を running にして実行キューへ投入する。
    - 実行は ThreadPoolExecutor で並列化する（主に LLM 呼び出しの待ち時間を隠す目的）。
    - apply_write_plan / tidy_memory は排他で実行する（DB更新が大きく衝突しやすいため）。
    - stale running は一定間隔で回収し、failed（スキップ）へ遷移する。
    """

    _ = periodic_interval_seconds  # cron無し定期は採用しない（ターン回数ベースを正とする）

    # --- 設定値の正規化 ---
    job_concurrency_i = max(1, int(job_concurrency))
    max_jobs_per_tick_i = max(1, int(max_jobs_per_tick))
    poll_interval_s = max(0.05, float(poll_interval_seconds))

    # --- 排他で実行したいジョブ種別 ---
    # NOTE:
    # - apply_write_plan は大量のDB更新 + 子ジョブenqueue を伴う。
    # - tidy_memory は多量のclose/link整理が入る。
    # - snapshot_runtime はスナップショット一貫性を優先して排他で扱う。
    # - これらは並列に走らせると sqlite ロック競合が増えやすく、失敗/遅延の原因になり得る。
    exclusive_kinds = {"apply_write_plan", "tidy_memory", "snapshot_runtime"}

    # --- ワーカースレッドプール ---
    # NOTE:
    # - Worker自体は別スレッド（internal_worker）で動く。
    # - ここでは「ジョブ実行」をさらに並列化し、LLM待ち時間を隠す。
    with ThreadPoolExecutor(max_workers=int(job_concurrency_i), thread_name_prefix="cocoro_ghost_job") as executor:
        in_flight: dict[Future[None], dict[str, Any]] = {}
        stop_deadline_ts: float | None = None
        next_stale_sweep_ts = 0.0

        # --- ループ ---
        while True:
            # --- 停止要求のチェック ---
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                # --- 新規の投入は止め、一定時間だけ完了を待つ（running stuckを増やさない） ---
                if stop_deadline_ts is None:
                    stop_deadline_ts = float(time.time()) + 2.0

            # --- 完了した future を回収 ---
            # NOTE:
            # - _run_one_job 自体はDBへ成功/失敗を書き戻す。
            # - ここでは「スレッドが落ちた」などの予期せぬ例外だけ拾ってログする。
            for fut, meta in list(in_flight.items()):
                if not fut.done():
                    continue
                try:
                    _ = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "job future crashed kind=%s job_id=%s",
                        str(meta.get("kind") or ""),
                        int(meta.get("job_id") or 0),
                        exc_info=exc,
                    )
                del in_flight[fut]

            # --- stale running 回収（プロセス再起動後の取り残しを復帰） ---
            now_float = float(time.time())
            if now_float >= float(next_stale_sweep_ts):
                in_flight_job_ids = {
                    int(meta.get("job_id") or 0)
                    for meta in in_flight.values()
                    if int(meta.get("job_id") or 0) > 0
                }
                requeued, dead_lettered = _recover_stale_running_jobs(
                    embedding_preset_id=str(embedding_preset_id),
                    embedding_dimension=int(embedding_dimension),
                    now_ts=_now_utc_ts(),
                    stale_seconds=int(_JOB_RUNNING_STALE_SECONDS),
                    ignore_job_ids=set(in_flight_job_ids),
                )
                if int(requeued) > 0 or int(dead_lettered) > 0:
                    logger.warning(
                        "worker recovered stale running jobs requeued=%s dead_lettered=%s stale_seconds=%s",
                        int(requeued),
                        int(dead_lettered),
                        int(_JOB_RUNNING_STALE_SECONDS),
                    )
                next_stale_sweep_ts = float(now_float) + float(_JOB_STALE_SWEEP_INTERVAL_SECONDS)

            # --- 停止要求中: in_flight が空なら終了 / 期限超過なら終了 ---
            if stop_deadline_ts is not None:
                if not in_flight:
                    return
                if float(time.time()) >= float(stop_deadline_ts):
                    return
                time.sleep(0.05)
                continue

            # --- 空き枠が無ければ待つ ---
            free_slots = int(job_concurrency_i) - len(in_flight)
            if free_slots <= 0:
                time.sleep(0.05)
                continue

            # --- 排他ジョブが待っているなら「新規投入を止めて」枠を空ける ---
            # NOTE:
            # - apply_write_plan / tidy_memory が pending のまま先頭に居ると、並列ジョブが増えていつまでも実行できない。
            # - そのため、排他ジョブが due の場合は、現在のin_flightを捌ききるまで新規投入を控える。
            if in_flight and _has_due_pending_job_kinds(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                kinds=set(exclusive_kinds),
            ):
                time.sleep(0.05)
                continue

            # --- 排他ジョブは in_flight が空の時だけ先に拾う ---
            if not in_flight:
                claimed_exclusive = _claim_due_jobs(
                    embedding_preset_id=str(embedding_preset_id),
                    embedding_dimension=int(embedding_dimension),
                    now_ts=_now_utc_ts(),
                    limit=1,
                    include_kinds=set(exclusive_kinds),
                    exclude_kinds=None,
                )
                if claimed_exclusive:
                    info = claimed_exclusive[0]
                    fut = executor.submit(
                        _run_one_job,
                        embedding_preset_id=str(embedding_preset_id),
                        embedding_dimension=int(embedding_dimension),
                        llm_client=llm_client,
                        job_id=int(info["job_id"]),
                    )
                    in_flight[fut] = dict(info)
                    logger.info(
                        "worker dispatched exclusive job kind=%s job_id=%s",
                        str(info.get("kind") or ""),
                        int(info.get("job_id") or 0),
                    )
                    continue

            # --- 通常ジョブを空き枠分だけ拾う（排他ジョブは除外） ---
            limit = min(int(max_jobs_per_tick_i), int(free_slots))
            claimed = _claim_due_jobs(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                now_ts=_now_utc_ts(),
                limit=int(limit),
                include_kinds=None,
                exclude_kinds=set(exclusive_kinds),
            )
            if claimed:
                for info in claimed:
                    fut = executor.submit(
                        _run_one_job,
                        embedding_preset_id=str(embedding_preset_id),
                        embedding_dimension=int(embedding_dimension),
                        llm_client=llm_client,
                        job_id=int(info["job_id"]),
                    )
                    in_flight[fut] = dict(info)
                logger.info(
                    "worker dispatched jobs=%s inflight=%s",
                    len(claimed),
                    len(in_flight),
                    extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
                )
                continue

            # --- 何も拾えない場合 ---
            # NOTE:
            # - in_flight があるなら、短く待って完了回収へ回る。
            # - 無いなら、通常のポーリング間隔で待つ。
            time.sleep(0.05 if in_flight else float(poll_interval_s))


def _has_due_pending_job_kinds(*, embedding_preset_id: str, embedding_dimension: int, kinds: set[str]) -> bool:
    """
    指定 kind の due(pending) ジョブが存在するかを返す。

    用途:
        - 排他ジョブ（apply_write_plan/tidy_memory）が待っている場合、
          追加の並列投入を控えてin_flightを早く空ける。
    """
    if not kinds:
        return False

    now_ts = _now_utc_ts()
    kinds_list = [str(k) for k in sorted({str(k).strip() for k in kinds if str(k).strip()})]
    if not kinds_list:
        return False

    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        n = (
            db.query(func.count(Job.id))
            .filter(Job.status == int(_JOB_PENDING))
            .filter(Job.run_after <= int(now_ts))
            .filter(Job.kind.in_(kinds_list))
            .scalar()
        )
    return int(n or 0) > 0


def _claim_due_jobs(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    now_ts: int,
    limit: int,
    include_kinds: set[str] | None,
    exclude_kinds: set[str] | None,
) -> list[dict[str, Any]]:
    """
    due(pending) jobs を running にして、実行対象のスナップショットを返す。

    注意:
        - ORMをセッション外に持ち出さないため、返すのは plain dict のみ。
        - ここで running にしたものは「必ず」どこかで実行される前提。
          そのため、スケジューリング判断（排他など）は呼び出し側で済ませる。
    """
    limit_i = max(1, int(limit))

    # --- kind フィルタの正規化 ---
    include_list: list[str] | None = None
    if include_kinds:
        tmp = [str(k).strip() for k in include_kinds if str(k).strip()]
        include_list = sorted(set(tmp)) if tmp else None
    exclude_list: list[str] | None = None
    if exclude_kinds:
        tmp = [str(k).strip() for k in exclude_kinds if str(k).strip()]
        exclude_list = sorted(set(tmp)) if tmp else None

    # --- due jobs を取る ---
    # NOTE:
    # - memory_session_scope は正常終了時に commit する。
    # - SQLAlchemy は commit で ORM オブジェクトを expire し得るため、
    #   with を抜けた後に rows（Job ORM）へ触ると DetachedInstanceError になり得る。
    # - そのため、外に持ち出すのは「スナップショット(dict)」だけにする。
    claimed: list[dict[str, Any]] = []
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        q = (
            db.query(Job)
            .filter(Job.status == int(_JOB_PENDING))
            .filter(Job.run_after <= int(now_ts))
            .order_by(Job.run_after.asc(), Job.id.asc())
        )

        # --- include_kinds があればその集合に絞る ---
        if include_list is not None:
            q = q.filter(Job.kind.in_(include_list))

        # --- exclude_kinds があればその集合を除外する ---
        if exclude_list is not None:
            q = q.filter(~Job.kind.in_(exclude_list))

        rows = q.limit(int(limit_i)).all()
        if not rows:
            return []

        # --- 実行対象を running にする（同時実行があっても二重処理を減らす） ---
        for j in rows:
            j.status = int(_JOB_RUNNING)
            j.updated_at = int(now_ts)
            db.add(j)
            claimed.append({"job_id": int(j.id), "kind": str(j.kind or "").strip()})

    return claimed


def _recover_stale_running_jobs(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    now_ts: int,
    stale_seconds: int,
    ignore_job_ids: set[int],
) -> tuple[int, int]:
    """
    stale な running ジョブを回収する。

    方針:
        - in_flight（現在このプロセスで実行中）の job_id は対象外にする。
        - stale と判定したものは tries を進め、failed（スキップ）へ遷移する。

    Returns:
        (requeued_count, dead_lettered_count)
    """

    # --- stale 判定閾値 ---
    stale_before_ts = int(now_ts) - max(1, int(stale_seconds))
    ignore_ids = sorted({int(x) for x in (ignore_job_ids or set()) if int(x) > 0})

    requeued = 0
    dead_lettered = 0
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        q = (
            db.query(Job)
            .filter(Job.status == int(_JOB_RUNNING))
            .filter(Job.updated_at <= int(stale_before_ts))
        )
        if ignore_ids:
            q = q.filter(~Job.id.in_(ignore_ids))
        rows = q.order_by(Job.updated_at.asc(), Job.id.asc()).all()
        if not rows:
            return (0, 0)

        # --- stale running は再試行せず failed（スキップ）へ遷移 ---
        for job in rows:
            old_tries = int(job.tries or 0)
            next_tries = int(old_tries) + 1
            job.status = int(_JOB_FAILED)
            job.tries = int(next_tries)
            job.updated_at = int(now_ts)
            job.last_error = f"stale running skipped on recovery tries={next_tries}"
            db.add(job)
            dead_lettered += 1

    return (int(requeued), int(dead_lettered))


def _run_one_job(*, embedding_preset_id: str, embedding_dimension: int, llm_client: LlmClient, job_id: int) -> None:
    """jobs.id を指定して1件実行する（成功/失敗をDBへ反映）。"""

    now_ts = _now_utc_ts()

    # --- job を読む ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        job = db.query(Job).filter(Job.id == int(job_id)).one_or_none()
        if job is None:
            return

        kind = str(job.kind or "").strip()
        payload = common_utils.json_loads_maybe(job.payload_json)

    # --- 実行 ---
    try:
        # --- 実行開始（観測用） ---
        logger.info(
            "job start kind=%s job_id=%s",
            kind,
            int(job_id),
            extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
        )
        # --- kindごとの実処理は worker_handlers へ委譲する ---
        run_job_kind(
            kind=str(kind),
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            payload=payload,
        )

        # --- 成功を書き戻す ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            job2 = db.query(Job).filter(Job.id == int(job_id)).one_or_none()
            if job2 is None:
                return
            job2.status = int(_JOB_DONE)
            job2.updated_at = int(now_ts)
            job2.last_error = None
            db.add(job2)

        # --- 実行完了（観測用） ---
        logger.info(
            "job done kind=%s job_id=%s",
            kind,
            int(job_id),
            extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
        )
    except Exception as exc:  # noqa: BLE001
        # --- 失敗時は即スキップ（再試行しない） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            job2 = db.query(Job).filter(Job.id == int(job_id)).one_or_none()
            if job2 is None:
                return
            job2.updated_at = int(now_ts)
            next_tries = int(job2.tries or 0) + 1
            job2.tries = int(next_tries)
            job2.status = int(_JOB_FAILED)
            job2.last_error = f"skipped on failure tries={next_tries} error={str(exc)}"
            db.add(job2)
        logger.warning(
            "job failed (skipped) kind=%s job_id=%s tries=%s error=%s",
            kind,
            int(job_id),
            int(next_tries),
            str(exc),
        )
