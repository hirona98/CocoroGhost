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
import json
import logging
import time
from typing import Any

from sqlalchemy import func, text

from cocoro_ghost import affect
from cocoro_ghost import common_utils, prompt_builders, vector_index
from cocoro_ghost import entity_utils
from cocoro_ghost.db import memory_session_scope, search_similar_item_ids, upsert_vec_item
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import (
    Event,
    EventAffect,
    EventAssistantSummary,
    EventEntity,
    EventLink,
    EventThread,
    Job,
    Revision,
    State,
    StateEntity,
    StateLink,
)
from cocoro_ghost.time_utils import format_iso8601_local, parse_iso8601_to_utc_ts


logger = logging.getLogger(__name__)


_JOB_PENDING = 0
_JOB_RUNNING = 1
_JOB_DONE = 2
_JOB_FAILED = 3


_TIDY_CHAT_TURNS_INTERVAL = 200
_TIDY_CHAT_TURNS_INTERVAL_FIRST = 10
_TIDY_MAX_CLOSE_PER_RUN = 200
_TIDY_ACTIVE_STATE_FETCH_LIMIT = 5000


def _now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""
    return int(time.time())


def _normalize_text_for_dedupe(text_in: str) -> str:
    """重複判定用に本文テキストを正規化する（空白の揺れを吸収）。"""
    s = str(text_in or "").strip()
    if not s:
        return ""

    # --- 空白の連続を1つにする（日本語でも不都合が少ない） ---
    return " ".join(s.split())


def _canonicalize_json_for_dedupe(text_in: str) -> str:
    """重複判定用に payload_json を安定化する（JSONならキー順を揃える）。"""
    s = str(text_in or "").strip()
    if not s:
        return ""

    # --- JSONとして読めるなら、キー順を揃えてダンプする ---
    try:
        obj = json.loads(s)
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return s


def _has_pending_or_running_job(db, *, kind: str) -> bool:
    """指定kindの pending/running ジョブが存在するかを返す。"""
    n = (
        db.query(func.count(Job.id))
        .filter(Job.kind == str(kind))
        .filter(Job.status.in_([int(_JOB_PENDING), int(_JOB_RUNNING)]))
        .scalar()
    )
    return int(n or 0) > 0


def _last_tidy_watermark_event_id(db) -> int:
    """最後に完了した tidy_memory が対象にした event_id の上限（watermark）を返す。"""
    last = (
        db.query(Job)
        .filter(Job.kind == "tidy_memory")
        .filter(Job.status == int(_JOB_DONE))
        .order_by(Job.id.desc())
        .first()
    )
    if last is None:
        return 0

    payload = common_utils.json_loads_maybe(str(last.payload_json or ""))
    try:
        return int(payload.get("watermark_event_id") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _enqueue_tidy_memory_job(
    *,
    db,
    now_ts: int,
    run_after: int,
    reason: str,
    watermark_event_id: int,
) -> None:
    """記憶整理ジョブ（tidy_memory）を投入する（重複投入は呼び出し側で防ぐ）。"""
    db.add(
        Job(
            kind="tidy_memory",
            payload_json=common_utils.json_dumps(
                {
                    "reason": str(reason),
                    "max_close_per_run": int(_TIDY_MAX_CLOSE_PER_RUN),
                    "watermark_event_id": int(watermark_event_id),
                }
            ),
            status=int(_JOB_PENDING),
            run_after=int(run_after),
            tries=0,
            last_error=None,
            created_at=int(now_ts),
            updated_at=int(now_ts),
        )
    )


def _build_event_embedding_text(ev: Event) -> str:
    """埋め込み対象テキストを組み立てる。"""
    parts: list[str] = []

    # --- user_text ---
    ut = str(ev.user_text or "").strip()
    if ut:
        parts.append(ut)

    # --- assistant_text ---
    at = str(ev.assistant_text or "").strip()
    if at:
        if parts:
            parts.append("")
        parts.append(at)

    # --- 画像要約（内部用） ---
    # NOTE:
    # - 画像そのものは保存しないが、要約は検索に効かせるため埋め込みへ含める。
    img_json = str(getattr(ev, "image_summaries_json", None) or "").strip()
    if img_json:
        try:
            obj = json.loads(img_json)
        except Exception:  # noqa: BLE001
            obj = None
        if isinstance(obj, list):
            summaries = [str(x or "").strip() for x in obj if str(x or "").strip()]
            if summaries:
                if parts:
                    parts.append("")
                parts.append("[画像要約]")
                parts.extend(summaries)

    # --- source は短く補助情報として付ける ---
    parts.append("")
    parts.append(f"(source={str(ev.source)})")

    # --- 長すぎる場合は頭側を優先して切る ---
    text_out = "\n".join([p for p in parts if p is not None]).strip()
    if len(text_out) > 8000:
        text_out = text_out[:8000]
    return text_out


def _build_state_embedding_text(st: State) -> str:
    """状態の埋め込み対象テキストを組み立てる。"""
    parts: list[str] = []

    # --- kind ---
    parts.append(f"(kind={str(st.kind)})")

    # --- body_text ---
    bt = str(st.body_text or "").strip()
    if bt:
        parts.append(bt)

    # --- payload は補助情報として短く添える ---
    pj = str(st.payload_json or "").strip()
    if pj:
        parts.append(f"(payload={pj[:1200]})")

    text_out = "\n".join([p for p in parts if p is not None]).strip()
    if len(text_out) > 8000:
        text_out = text_out[:8000]
    return text_out


def _build_event_affect_embedding_text(aff: EventAffect) -> str:
    """イベント感情の埋め込み対象テキストを組み立てる。"""
    parts: list[str] = []

    # --- moment_affect_text ---
    t = common_utils.strip_face_tags(str(aff.moment_affect_text or "").strip())
    if t:
        parts.append(t)

    # --- moment_affect_labels ---
    labels = common_utils.parse_json_str_list(str(getattr(aff, "moment_affect_labels_json", "") or ""))
    if labels:
        parts.append("")
        parts.append(f"【ラベル】 {', '.join(labels[:6])}")

    # --- inner_thought_text ---
    it = common_utils.strip_face_tags(str(aff.inner_thought_text or "").strip())
    if it:
        parts.append("")
        parts.append(f"【内心】 {it}")

    # --- VAD ---
    parts.append("")
    parts.append(f"(vad v={float(aff.vad_v):.3f} a={float(aff.vad_a):.3f} d={float(aff.vad_d):.3f})")

    text_out = "\n".join([p for p in parts if p is not None]).strip()
    if len(text_out) > 8000:
        text_out = text_out[:8000]
    return text_out


def _build_event_assistant_summary_input(ev: Event) -> str:
    """要約生成のための入力テキストを組み立てる。

    方針:
        - 事実追加を防ぐため、材料は events の本文（user/assistant/画像要約）だけに限定する。
        - LLMが読みやすいようにラベルを付ける。
        - 長文は切り詰める（workerでのコスト暴れを防ぐ）。
    """

    parts: list[str] = []

    # --- user_text ---
    ut = str(ev.user_text or "").strip()
    if ut:
        parts.append("[user_text]")
        parts.append(ut)

    # --- assistant_text ---
    at = common_utils.strip_face_tags(str(ev.assistant_text or "").strip())
    if at:
        if parts:
            parts.append("")
        parts.append("[assistant_text]")
        parts.append(at)

    # --- 画像要約（内部用） ---
    img_json = str(getattr(ev, "image_summaries_json", None) or "").strip()
    if img_json:
        try:
            obj = json.loads(img_json)
        except Exception:  # noqa: BLE001
            obj = None
        if isinstance(obj, list):
            summaries = [str(x or "").strip() for x in obj if str(x or "").strip()]
            if summaries:
                if parts:
                    parts.append("")
                parts.append("[image_summaries]")
                parts.extend(summaries[:5])

    text_out = "\n".join([p for p in parts if p is not None]).strip()

    # --- 長すぎる場合は頭側を優先して切る（要約の目的は「何の話か」） ---
    if len(text_out) > 6000:
        text_out = text_out[:6000]
    return text_out


def _state_row_to_json(st: State) -> dict[str, Any]:
    """State行をJSON化する（LLM入力/Revision保存の両方で使う）。"""
    return {
        "state_id": int(st.state_id),
        "kind": str(st.kind),
        "body_text": str(st.body_text),
        "payload_json": str(st.payload_json),
        "last_confirmed_at": int(st.last_confirmed_at),
        "confidence": float(st.confidence),
        "salience": float(st.salience),
        "valid_from_ts": (int(st.valid_from_ts) if st.valid_from_ts is not None else None),
        "valid_to_ts": (int(st.valid_to_ts) if st.valid_to_ts is not None else None),
        "created_at": int(st.created_at),
        "updated_at": int(st.updated_at),
    }


def _link_row_to_json(link: EventLink) -> dict[str, Any]:
    """EventLink行をJSON化する（Revision保存用）。"""
    return {
        "id": int(link.id),
        "from_event_id": int(link.from_event_id),
        "to_event_id": int(link.to_event_id),
        "label": str(link.label),
        "confidence": float(link.confidence),
        "evidence_event_ids_json": str(link.evidence_event_ids_json),
        "created_at": int(link.created_at),
    }


def _state_link_row_to_json(link: StateLink) -> dict[str, Any]:
    """StateLink行をJSON化する（Revision保存用）。"""
    return {
        "id": int(link.id),
        "from_state_id": int(link.from_state_id),
        "to_state_id": int(link.to_state_id),
        "label": str(link.label),
        "confidence": float(link.confidence),
        "evidence_event_ids_json": str(link.evidence_event_ids_json),
        "created_at": int(link.created_at),
    }


def _thread_row_to_json(th: EventThread) -> dict[str, Any]:
    """EventThread行をJSON化する（Revision保存用）。"""
    return {
        "id": int(th.id),
        "event_id": int(th.event_id),
        "thread_key": str(th.thread_key),
        "confidence": float(th.confidence),
        "evidence_event_ids_json": str(th.evidence_event_ids_json),
        "created_at": int(th.created_at),
    }


def _affect_row_to_json(aff: EventAffect) -> dict[str, Any]:
    """EventAffect行をJSON化する（Revision保存用）。"""
    return {
        "id": int(aff.id),
        "event_id": int(aff.event_id),
        "created_at": int(aff.created_at),
        "moment_affect_text": str(aff.moment_affect_text),
        "moment_affect_labels_json": str(aff.moment_affect_labels_json),
        "inner_thought_text": (str(aff.inner_thought_text) if aff.inner_thought_text is not None else None),
        "vad_v": float(aff.vad_v),
        "vad_a": float(aff.vad_a),
        "vad_d": float(aff.vad_d),
        "confidence": float(aff.confidence),
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
    # - これらは並列に走らせると sqlite ロック競合が増えやすく、失敗/遅延の原因になり得る。
    exclusive_kinds = {"apply_write_plan", "tidy_memory"}

    # --- ワーカースレッドプール ---
    # NOTE:
    # - Worker自体は別スレッド（internal_worker）で動く。
    # - ここでは「ジョブ実行」をさらに並列化し、LLM待ち時間を隠す。
    with ThreadPoolExecutor(max_workers=int(job_concurrency_i), thread_name_prefix="cocoro_ghost_job") as executor:
        in_flight: dict[Future[None], dict[str, Any]] = {}
        stop_deadline_ts: float | None = None

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
        if kind == "upsert_event_embedding":
            _handle_upsert_event_embedding(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                llm_client=llm_client,
                payload=payload,
            )
        elif kind == "generate_write_plan":
            _handle_generate_write_plan(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                llm_client=llm_client,
                payload=payload,
            )
        elif kind == "apply_write_plan":
            _handle_apply_write_plan(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                payload=payload,
            )
        elif kind == "upsert_state_embedding":
            _handle_upsert_state_embedding(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                llm_client=llm_client,
                payload=payload,
            )
        elif kind == "upsert_event_affect_embedding":
            _handle_upsert_event_affect_embedding(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                llm_client=llm_client,
                payload=payload,
            )
        elif kind == "upsert_event_assistant_summary":
            _handle_upsert_event_assistant_summary(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                llm_client=llm_client,
                payload=payload,
            )
        elif kind == "tidy_memory":
            _handle_tidy_memory(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                payload=payload,
            )
        elif kind == "build_state_links":
            _handle_build_state_links(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
                llm_client=llm_client,
                payload=payload,
            )
        else:
            raise RuntimeError(f"unknown job kind: {kind}")

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
        # --- 失敗を書き戻す ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            job2 = db.query(Job).filter(Job.id == int(job_id)).one_or_none()
            if job2 is None:
                return
            job2.status = int(_JOB_FAILED)
            job2.updated_at = int(now_ts)
            job2.tries = int(job2.tries or 0) + 1
            job2.last_error = str(exc)
            db.add(job2)
        logger.warning("job failed kind=%s job_id=%s error=%s", kind, int(job_id), str(exc))


def _handle_upsert_event_embedding(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """events の埋め込みを生成し vec_items へ upsert する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")

    # --- event を読む ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        # --- 検索対象外（自動分離など）なら、vec_items を消して終了 ---
        # NOTE:
        # - vec_items が残ると、ベクトル検索で再浮上する可能性がある。
        # - searchable=0 は「ログは残すが、想起には使わない」を表す。
        if int(getattr(ev, "searchable", 1) or 0) != 1:
            item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT), int(event_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_event_embedding_text(ev)
        rank_at = int(ev.created_at)

    if not text_in:
        return

    # --- embedding を作る ---
    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_EVENT_EMBEDDING)[0]

    # --- vec_items へ書く（kind+entity_idの衝突を避ける） ---
    item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT), int(event_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(vector_index.VEC_KIND_EVENT),
            rank_at=int(rank_at),
            active=1,
        )


def _handle_generate_write_plan(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """WritePlan を生成し、apply_write_plan ジョブを投入する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")

    # --- ランタイム設定（ペルソナ/呼称）を取得する ---
    # NOTE:
    # - WritePlan は内部用だが、記憶（state_updates / event_affect）に残る文章は人格の口調に揃える。
    # - 初期化順や例外の影響を避けるため、取れない場合は最小の既定値へフォールバックする。
    persona_text = ""
    addon_text = ""
    second_person_label = "あなた"
    try:
        from cocoro_ghost.config import get_config_store

        cfg = get_config_store().config
        persona_text = str(getattr(cfg, "persona_text", "") or "")
        addon_text = str(getattr(cfg, "addon_text", "") or "")
        second_person_label = str(getattr(cfg, "second_person_label", "") or "").strip() or "あなた"
    except Exception:  # noqa: BLE001
        persona_text = ""
        addon_text = ""
        second_person_label = "あなた"

    # --- event + 周辺コンテキストを集める（過剰に重くしない） ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        # --- 検索対象外（自動分離など）なら、記憶更新の材料にしない ---
        if int(getattr(ev, "searchable", 1) or 0) != 1:
            return

        # --- 画像要約（events.image_summaries_json）をlist[str]へ正規化する ---
        # NOTE:
        # - 画像そのものは保存しないが、要約は状態更新/文脈/要約に効かせるため、WritePlan入力へ含める。
        def _image_summaries_list(image_summaries_json: Any) -> list[str]:
            s = str(image_summaries_json or "").strip()
            if not s:
                return []
            try:
                obj = json.loads(s)
            except Exception:  # noqa: BLE001
                return []
            if not isinstance(obj, list):
                return []
            return [str(x or "").strip()[:400] for x in obj if str(x or "").strip()]

        # --- client_context_json をdictへ正規化する（観測の手がかりとして渡す） ---
        def _client_context_dict(client_context_json: Any) -> dict[str, Any] | None:
            s = str(client_context_json or "").strip()
            if not s:
                return None
            try:
                obj = json.loads(s)
            except Exception:  # noqa: BLE001
                return None
            if not isinstance(obj, dict):
                return None
            # NOTE: 過剰に膨らませない（必要最小限）。
            return {
                "active_app": str(obj.get("active_app") or "").strip(),
                "window_title": str(obj.get("window_title") or "").strip(),
                "locale": str(obj.get("locale") or "").strip(),
            }

        # --- 観測メタ（desktop_watch/vision_detail向け） ---
        observation_meta: dict[str, Any] | None = None
        if str(ev.source) in {"desktop_watch", "vision_detail"}:
            # NOTE: 行為主体の誤認防止のため、二人称呼称は設定値で明示する（ペルソナ本文からはパースしない）。
            observation_meta = {
                "observer": "self",
                "second_person_label": str(second_person_label),
                "user_text_role": "screen_description",
            }

        event_snapshot = {
            "event_id": int(ev.event_id),
            "created_at": format_iso8601_local(int(ev.created_at)),
            "source": str(ev.source),
            "client_id": (str(ev.client_id) if ev.client_id is not None else None),
            "user_text": str(ev.user_text or ""),
            "assistant_text": str(ev.assistant_text or ""),
            "image_summaries": _image_summaries_list(getattr(ev, "image_summaries_json", None)),
            "client_context": _client_context_dict(getattr(ev, "client_context_json", None)),
            "observation": observation_meta,
        }

        # --- 直近の出来事（同一client優先） ---
        recent_events_q = db.query(Event).filter(Event.searchable == 1).order_by(Event.event_id.desc())
        if ev.client_id is not None and str(ev.client_id).strip():
            recent_events_q = recent_events_q.filter(Event.client_id == str(ev.client_id))
        recent_events = recent_events_q.limit(12).all()

        # --- 直近の状態 ---
        recent_states = (
            db.query(State)
            .filter(State.searchable == 1)
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .limit(24)
            .all()
        )

        recent_event_snapshots = [
            {
                "event_id": int(x.event_id),
                "created_at": format_iso8601_local(int(x.created_at)),
                "source": str(x.source),
                "user_text": str(x.user_text or "")[:1000],
                "assistant_text": str(x.assistant_text or "")[:1000],
                "image_summaries_preview": (
                    "\n".join(_image_summaries_list(getattr(x, "image_summaries_json", None)))[:1000] or None
                ),
            }
            for x in recent_events
        ]
        recent_state_snapshots = [
            {
                "state_id": int(s.state_id),
                "kind": str(s.kind),
                "body_text": str(s.body_text)[:1000],
                "payload_json": str(s.payload_json)[:1000],
                "last_confirmed_at": format_iso8601_local(int(s.last_confirmed_at)),
                # NOTE: DBはUNIX秒だが、LLMにはISOで渡す。
                "valid_from_ts": (
                    format_iso8601_local(int(s.valid_from_ts))
                    if s.valid_from_ts is not None and int(s.valid_from_ts) > 0
                    else None
                ),
                "valid_to_ts": (
                    format_iso8601_local(int(s.valid_to_ts))
                    if s.valid_to_ts is not None and int(s.valid_to_ts) > 0
                    else None
                ),
            }
            for s in recent_states
        ]

    input_obj: dict[str, Any] = {
        "event": event_snapshot,
        "recent_events": recent_event_snapshots,
        "recent_states": recent_state_snapshots,
    }

    # --- LLMでWritePlanを作る ---
    resp = llm_client.generate_json_response(
        system_prompt=prompt_builders.write_plan_system_prompt(
            persona_text=persona_text,
            second_person_label=second_person_label,
        ),
        input_text=common_utils.json_dumps(input_obj),
        purpose=LlmRequestPurpose.ASYNC_WRITE_PLAN,
        max_tokens=2400,
    )
    content = ""
    try:
        content = str(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        try:
            content = str(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            content = ""
    plan_obj = common_utils.parse_first_json_object_or_raise(content)

    # --- apply_write_plan を投入する（planはpayloadに入れて監査できるようにする） ---
    now_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.add(
            Job(
                kind="apply_write_plan",
                payload_json=common_utils.json_dumps({"event_id": int(event_id), "write_plan": plan_obj}),
                status=int(_JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )


def _handle_apply_write_plan(*, embedding_preset_id: str, embedding_dimension: int, payload: dict[str, Any]) -> None:
    """WritePlan を適用し、状態/感情/文脈グラフを更新する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    plan = payload.get("write_plan")
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")
    if not isinstance(plan, dict):
        raise RuntimeError("payload.write_plan is required")

    now_ts = _now_utc_ts()

    # --- まとめて1トランザクションで適用する（途中失敗で半端に残さない） ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- LongMoodState 用に、今回の event_affect をメモしておく ---
        # NOTE:
        # - long_mood_state は「基調（baseline）」と「余韻（shock）」を分けて育てる。
        # - event_affect（瞬間反応）を入力として、shock を即時反映し、baseline は日スケールでゆっくり追従させる。
        moment_vad: dict[str, float] | None = None
        moment_conf: float = 0.0
        moment_note: str | None = None
        baseline_text_candidate: str | None = None

        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        base_ts = int(ev.created_at)

        # --- entity索引 更新ヘルパ（events/state） ---
        # NOTE:
        # - `events.entities_json` は監査/表示向けのスナップショットとして残す。
        # - 検索は `event_entities/state_entities` の正規化キー（type + name_norm）を正にする。
        def replace_event_entities(*, event_id_in: int, entities_norm_in: list[dict[str, Any]]) -> None:
            """event_entities を event_id 単位で作り直す（delete→insert）。"""

            # --- 既存を削除 ---
            db.query(EventEntity).filter(EventEntity.event_id == int(event_id_in)).delete(synchronize_session=False)

            # --- 新規を追加 ---
            for ent in entities_norm_in:
                t_norm = str(ent.get("type") or "").strip()
                name_raw = str(ent.get("name") or "").strip()
                if not t_norm or not name_raw:
                    continue
                name_norm = entity_utils.normalize_entity_name(name_raw)
                if not name_norm:
                    continue
                db.add(
                    EventEntity(
                        event_id=int(event_id_in),
                        entity_type_norm=str(t_norm),
                        entity_name_raw=str(name_raw),
                        entity_name_norm=str(name_norm),
                        confidence=float(entity_utils.clamp_01(ent.get("confidence"))),
                        created_at=int(base_ts),
                    )
                )

        def replace_state_entities(*, state_id_in: int, entities_norm_in: list[dict[str, Any]]) -> None:
            """state_entities を state_id 単位で作り直す（delete→insert）。"""

            # --- 既存を削除 ---
            db.query(StateEntity).filter(StateEntity.state_id == int(state_id_in)).delete(synchronize_session=False)

            # --- 新規を追加 ---
            for ent in entities_norm_in:
                t_norm = str(ent.get("type") or "").strip()
                name_raw = str(ent.get("name") or "").strip()
                if not t_norm or not name_raw:
                    continue
                name_norm = entity_utils.normalize_entity_name(name_raw)
                if not name_norm:
                    continue
                db.add(
                    StateEntity(
                        state_id=int(state_id_in),
                        entity_type_norm=str(t_norm),
                        entity_name_raw=str(name_raw),
                        entity_name_norm=str(name_norm),
                        confidence=float(entity_utils.clamp_01(ent.get("confidence"))),
                        created_at=int(base_ts),
                    )
                )

        # --- events 注釈更新（about_time/entities） ---
        ann = plan.get("event_annotations") if isinstance(plan, dict) else None
        entities_norm: list[dict[str, Any]] = []
        if isinstance(ann, dict):
            # NOTE: LLMはISO文字列を返す可能性があるため、ここでUNIX秒へ正規化する。
            a0 = ann.get("about_start_ts")
            a1 = ann.get("about_end_ts")
            a0_i = int(a0) if isinstance(a0, (int, float)) and int(a0) > 0 else parse_iso8601_to_utc_ts(str(a0) if a0 is not None else None)
            a1_i = int(a1) if isinstance(a1, (int, float)) and int(a1) > 0 else parse_iso8601_to_utc_ts(str(a1) if a1 is not None else None)
            ev.about_start_ts = int(a0_i) if a0_i is not None and int(a0_i) > 0 else None
            ev.about_end_ts = int(a1_i) if a1_i is not None and int(a1_i) > 0 else None

            y0 = ann.get("about_year_start")
            y1 = ann.get("about_year_end")
            ev.about_year_start = int(y0) if isinstance(y0, (int, float)) and int(y0) > 0 else None
            ev.about_year_end = int(y1) if isinstance(y1, (int, float)) and int(y1) > 0 else None
            life_stage = ann.get("life_stage")
            ev.life_stage = (str(life_stage) if life_stage is not None else None)
            ev.about_time_confidence = float(ann.get("about_time_confidence") or 0.0)
            # --- entities を正規化し、イベントへ保存 + 索引へ落とす ---
            entities_raw = ann.get("entities") if isinstance(ann.get("entities"), list) else []
            entities_norm = entity_utils.normalize_entities(entities_raw)
            ev.entities_json = common_utils.json_dumps(entities_norm)
            replace_event_entities(event_id_in=int(event_id), entities_norm_in=list(entities_norm))
        ev.updated_at = int(now_ts)
        db.add(ev)

        # --- revisions を追加するヘルパ ---
        def add_revision(*, entity_type: str, entity_id: int, before: Any, after: Any, reason: str, evidence_event_ids: list[int]) -> None:
            db.add(
                Revision(
                    entity_type=str(entity_type),
                    entity_id=int(entity_id),
                    before_json=(common_utils.json_dumps(before) if before is not None else None),
                    after_json=(common_utils.json_dumps(after) if after is not None else None),
                    reason=str(reason or "").strip() or "(no reason)",
                    evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_event_ids if int(x) > 0]),
                    created_at=int(now_ts),
                )
            )

        # --- event_affect（瞬間的な感情/内心） ---
        ea = plan.get("event_affect") if isinstance(plan, dict) else None
        if isinstance(ea, dict):
            moment_text = common_utils.strip_face_tags(str(ea.get("moment_affect_text") or "").strip())
            if moment_text:
                score = ea.get("moment_affect_score_vad") if isinstance(ea.get("moment_affect_score_vad"), dict) else {}
                conf = float(ea.get("moment_affect_confidence") or 0.0)
                labels = affect.sanitize_moment_affect_labels(ea.get("moment_affect_labels"))
                inner = ea.get("inner_thought_text")
                inner_text_raw = str(inner).strip() if inner is not None and str(inner).strip() else None
                inner_text = (
                    common_utils.strip_face_tags(inner_text_raw) if inner_text_raw is not None else None
                ) or None

                # --- LongMoodState 更新用のメモ（保存の成否に依存しない入力） ---
                moment_vad = affect.vad_dict(score.get("v"), score.get("a"), score.get("d"))
                moment_conf = affect.clamp_01(conf)
                moment_note = str(moment_text)[:240]

                # NOTE: 1 event につき1件として扱い、既存があれば更新する（重複を避ける）
                existing = (
                    db.query(EventAffect)
                    .filter(EventAffect.event_id == int(event_id))
                    .order_by(EventAffect.id.desc())
                    .first()
                )
                if existing is None:
                    aff = EventAffect(
                        event_id=int(event_id),
                        created_at=int(base_ts),
                        moment_affect_text=str(moment_text),
                        moment_affect_labels_json=common_utils.json_dumps(labels),
                        inner_thought_text=inner_text,
                        vad_v=affect.clamp_vad(score.get("v")),
                        vad_a=affect.clamp_vad(score.get("a")),
                        vad_d=affect.clamp_vad(score.get("d")),
                        confidence=float(conf),
                    )
                    db.add(aff)
                    db.flush()
                    add_revision(
                        entity_type="event_affects",
                        entity_id=int(aff.id),
                        before=None,
                        after=_affect_row_to_json(aff),
                        reason="event_affect を保存した",
                        evidence_event_ids=[int(event_id)],
                    )
                    affect_id = int(aff.id)
                else:
                    before = _affect_row_to_json(existing)
                    existing.moment_affect_text = str(moment_text)
                    existing.moment_affect_labels_json = common_utils.json_dumps(labels)
                    existing.inner_thought_text = inner_text
                    existing.vad_v = affect.clamp_vad(score.get("v"))
                    existing.vad_a = affect.clamp_vad(score.get("a"))
                    existing.vad_d = affect.clamp_vad(score.get("d"))
                    existing.confidence = float(conf)
                    db.add(existing)
                    db.flush()
                    add_revision(
                        entity_type="event_affects",
                        entity_id=int(existing.id),
                        before=before,
                        after=_affect_row_to_json(existing),
                        reason="event_affect を更新した",
                        evidence_event_ids=[int(event_id)],
                    )
                    affect_id = int(existing.id)

                # --- event_affect embedding job ---
                db.add(
                    Job(
                        kind="upsert_event_affect_embedding",
                        payload_json=common_utils.json_dumps({"affect_id": int(affect_id)}),
                        status=int(_JOB_PENDING),
                        run_after=int(now_ts),
                        tries=0,
                        last_error=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                )

        # --- 文脈グラフ（threads/links） ---
        cu = plan.get("context_updates") if isinstance(plan, dict) else None
        if isinstance(cu, dict):
            threads = cu.get("threads") if isinstance(cu.get("threads"), list) else []
            links = cu.get("links") if isinstance(cu.get("links"), list) else []

            # --- threads ---
            for t in threads:
                if not isinstance(t, dict):
                    continue
                thread_key = str(t.get("thread_key") or "").strip()
                if not thread_key:
                    continue
                confidence = float(t.get("confidence") or 0.0)

                existing = (
                    db.query(EventThread)
                    .filter(EventThread.event_id == int(event_id))
                    .filter(EventThread.thread_key == str(thread_key))
                    .one_or_none()
                )
                if existing is None:
                    th = EventThread(
                        event_id=int(event_id),
                        thread_key=str(thread_key),
                        confidence=float(confidence),
                        evidence_event_ids_json=common_utils.json_dumps([int(event_id)]),
                        created_at=int(now_ts),
                    )
                    db.add(th)
                    db.flush()
                    add_revision(
                        entity_type="event_threads",
                        entity_id=int(th.id),
                        before=None,
                        after=_thread_row_to_json(th),
                        reason="文脈スレッドを付与した",
                        evidence_event_ids=[int(event_id)],
                    )
                else:
                    before = _thread_row_to_json(existing)
                    existing.confidence = float(confidence)
                    existing.evidence_event_ids_json = common_utils.json_dumps([int(event_id)])
                    db.add(existing)
                    db.flush()
                    add_revision(
                        entity_type="event_threads",
                        entity_id=int(existing.id),
                        before=before,
                        after=_thread_row_to_json(existing),
                        reason="文脈スレッドの信頼度を更新した",
                        evidence_event_ids=[int(event_id)],
                    )

            # --- links ---
            for l in links:
                if not isinstance(l, dict):
                    continue
                to_event_id = int(l.get("to_event_id") or 0)
                label = str(l.get("label") or "").strip()
                if to_event_id <= 0 or not label:
                    continue
                confidence = float(l.get("confidence") or 0.0)

                existing = (
                    db.query(EventLink)
                    .filter(EventLink.from_event_id == int(event_id))
                    .filter(EventLink.to_event_id == int(to_event_id))
                    .filter(EventLink.label == str(label))
                    .one_or_none()
                )
                if existing is None:
                    link = EventLink(
                        from_event_id=int(event_id),
                        to_event_id=int(to_event_id),
                        label=str(label),
                        confidence=float(confidence),
                        evidence_event_ids_json=common_utils.json_dumps([int(event_id)]),
                        created_at=int(now_ts),
                    )
                    db.add(link)
                    db.flush()
                    add_revision(
                        entity_type="event_links",
                        entity_id=int(link.id),
                        before=None,
                        after=_link_row_to_json(link),
                        reason="文脈リンクを追加した",
                        evidence_event_ids=[int(event_id)],
                    )
                else:
                    before = _link_row_to_json(existing)
                    existing.confidence = float(confidence)
                    existing.evidence_event_ids_json = common_utils.json_dumps([int(event_id)])
                    db.add(existing)
                    db.flush()
                    add_revision(
                        entity_type="event_links",
                        entity_id=int(existing.id),
                        before=before,
                        after=_link_row_to_json(existing),
                        reason="文脈リンクの信頼度を更新した",
                        evidence_event_ids=[int(event_id)],
                    )

        # --- state 更新 ---
        # NOTE:
        # - state_links（B-1）のリンク生成は非同期ジョブで行う。
        # - ここでは「今回更新したstate_id」を集めて、後段で build_state_links を投入する。
        updated_state_ids_for_links: set[int] = set()
        su = plan.get("state_updates") if isinstance(plan, dict) else None
        if isinstance(su, list):
            for u in su:
                if not isinstance(u, dict):
                    continue

                # --- state_entities 用の entities（state単位） ---
                # NOTE:
                # - 以前は event_annotations.entities を stateへ流用していたが、精度が粗くなりやすい。
                # - 本来は「stateごとに関係するentity」を付与するのが自然なので、WritePlanで分けて出す。
                entities_norm_for_state = entity_utils.normalize_entities(u.get("entities"))

                op = str(u.get("op") or "").strip()
                kind = str(u.get("kind") or "").strip()
                reason = str(u.get("reason") or "").strip() or "(no reason)"
                evidence = u.get("evidence_event_ids") if isinstance(u.get("evidence_event_ids"), list) else [int(event_id)]
                evidence_ids = [int(x) for x in evidence if int(x) > 0]
                if int(event_id) not in evidence_ids:
                    evidence_ids.append(int(event_id))

                state_id = u.get("state_id")
                state_id_i = int(state_id) if isinstance(state_id, (int, float)) and int(state_id) > 0 else None

                # --- last_confirmed_at は event 時刻を正とする（LLMが時刻を作ると破綻しやすい） ---
                # NOTE: 会話の時系列と最近性（検索順位）を壊さないため、常に base_ts を使う。
                last_confirmed_at_i = int(base_ts)

                valid_from_ts = u.get("valid_from_ts")
                valid_to_ts = u.get("valid_to_ts")
                # NOTE: LLMはISO文字列を返す可能性があるため、ここでUNIX秒へ正規化する。
                v_from = (
                    int(valid_from_ts)
                    if isinstance(valid_from_ts, (int, float)) and int(valid_from_ts) > 0
                    else parse_iso8601_to_utc_ts(str(valid_from_ts) if valid_from_ts is not None else None)
                )
                v_to = (
                    int(valid_to_ts)
                    if isinstance(valid_to_ts, (int, float)) and int(valid_to_ts) > 0
                    else parse_iso8601_to_utc_ts(str(valid_to_ts) if valid_to_ts is not None else None)
                )

                confidence = float(u.get("confidence") or 0.0)
                salience = float(u.get("salience") or 0.0)
                body_text = str(u.get("body_text") or "").strip()
                payload_obj = u.get("payload") if isinstance(u.get("payload"), dict) else {}

                # --- kind/op の最低限チェック ---
                if not op or not kind:
                    continue

                # --- close/mark_done は state_id 必須 ---
                if op in ("close", "mark_done") and state_id_i is None:
                    continue

                if op == "upsert":
                    # --- long_mood_state は「本文案」だけ拾う（数値はサーバ側で安定化する） ---
                    # NOTE:
                    # - long_mood_state を LLM の本文で毎ターン差し替えると、短期すぎる背景になりやすい。
                    # - baseline/shock の数値モデルはサーバで決め、本文（baseline_text）は日スケールで更新する。
                    if str(kind) == "long_mood_state":
                        if body_text:
                            baseline_text_candidate = str(body_text)
                        # NOTE: ここでは upsert を続行せず、後段の「LongMoodState 統合更新」で扱う。
                        continue

                    # --- 既存を更新するか、新規を作る ---
                    existing = db.query(State).filter(State.state_id == int(state_id_i)).one_or_none() if state_id_i else None

                    # --- long_mood_state は「単一更新」で育てる ---
                    # NOTE:
                    # - long_mood_state は「背景」として扱い、複数を並存させない（増殖させない）
                    # - 整理（tidy_memory）では long_mood_state を触らない前提なので、ここで増殖を防ぐ
                    if str(kind) == "long_mood_state":
                        if existing is not None and str(existing.kind) != "long_mood_state":
                            existing = None

                        if existing is None:
                            existing = (
                                db.query(State)
                                .filter(State.kind == "long_mood_state")
                                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                                .first()
                            )

                    if existing is None:
                        if not body_text:
                            body_text = common_utils.json_dumps(payload_obj)[:2000]
                        st = State(
                            kind=str(kind),
                            body_text=str(body_text),
                            payload_json=common_utils.json_dumps(payload_obj),
                            last_confirmed_at=int(last_confirmed_at_i),
                            confidence=float(confidence),
                            salience=float(salience),
                            valid_from_ts=v_from,
                            valid_to_ts=v_to,
                            created_at=int(base_ts),
                            updated_at=int(now_ts),
                        )
                        db.add(st)
                        db.flush()
                        add_revision(
                            entity_type="state",
                            entity_id=int(st.state_id),
                            before=None,
                            after=_state_row_to_json(st),
                            reason=str(reason),
                            evidence_event_ids=evidence_ids,
                        )
                        changed_state_id = int(st.state_id)
                    else:
                        before = _state_row_to_json(existing)
                        existing.kind = str(kind)
                        if body_text:
                            existing.body_text = str(body_text)
                        existing.payload_json = common_utils.json_dumps(payload_obj)
                        existing.last_confirmed_at = int(last_confirmed_at_i)
                        existing.confidence = float(confidence)
                        existing.salience = float(salience)
                        existing.valid_from_ts = v_from
                        existing.valid_to_ts = v_to
                        existing.updated_at = int(now_ts)
                        db.add(existing)
                        db.flush()
                        add_revision(
                            entity_type="state",
                            entity_id=int(existing.state_id),
                            before=before,
                            after=_state_row_to_json(existing),
                            reason=str(reason),
                            evidence_event_ids=evidence_ids,
                        )
                        changed_state_id = int(existing.state_id)

                    # --- state entity索引（後で一括で付与） ---
                    if int(changed_state_id) > 0:
                        # --- state単位のentitiesを付与（delete→insert） ---
                        replace_state_entities(
                            state_id_in=int(changed_state_id),
                            entities_norm_in=list(entities_norm_for_state),
                        )
                        updated_state_ids_for_links.add(int(changed_state_id))

                    # --- state embedding job ---
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=common_utils.json_dumps({"state_id": int(changed_state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_ts),
                            updated_at=int(now_ts),
                        )
                    )

                elif op == "close":
                    st = db.query(State).filter(State.state_id == int(state_id_i)).one_or_none()
                    if st is None:
                        continue
                    before = _state_row_to_json(st)
                    # NOTE: valid_to が無い場合は event の about_end → 無ければ event時刻
                    st.valid_to_ts = v_to if v_to is not None else (int(ev.about_end_ts) if ev.about_end_ts is not None else int(base_ts))
                    st.last_confirmed_at = int(last_confirmed_at_i)
                    st.updated_at = int(now_ts)
                    db.add(st)
                    db.flush()
                    add_revision(
                        entity_type="state",
                        entity_id=int(st.state_id),
                        before=before,
                        after=_state_row_to_json(st),
                        reason=str(reason),
                        evidence_event_ids=evidence_ids,
                    )
                    # --- state entity索引（後で一括で付与） ---
                    if str(st.kind) != "long_mood_state":
                        replace_state_entities(
                            state_id_in=int(st.state_id),
                            entities_norm_in=list(entities_norm_for_state),
                        )
                        updated_state_ids_for_links.add(int(st.state_id))
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=common_utils.json_dumps({"state_id": int(st.state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_ts),
                            updated_at=int(now_ts),
                        )
                    )

                elif op == "mark_done":
                    st = db.query(State).filter(State.state_id == int(state_id_i)).one_or_none()
                    if st is None:
                        continue
                    before = _state_row_to_json(st)
                    # --- payload の status を done にする ---
                    pj = common_utils.json_loads_maybe(st.payload_json)
                    pj["status"] = "done"
                    st.payload_json = common_utils.json_dumps(pj)
                    st.last_confirmed_at = int(last_confirmed_at_i)
                    st.updated_at = int(now_ts)
                    db.add(st)
                    db.flush()
                    add_revision(
                        entity_type="state",
                        entity_id=int(st.state_id),
                        before=before,
                        after=_state_row_to_json(st),
                        reason=str(reason),
                        evidence_event_ids=evidence_ids,
                    )
                    # --- state entity索引（後で一括で付与） ---
                    if str(st.kind) != "long_mood_state":
                        replace_state_entities(
                            state_id_in=int(st.state_id),
                            entities_norm_in=list(entities_norm_for_state),
                        )
                        updated_state_ids_for_links.add(int(st.state_id))
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=common_utils.json_dumps({"state_id": int(st.state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
                            tries=0,
                            last_error=None,
                            created_at=int(now_ts),
                            updated_at=int(now_ts),
                        )
                    )

        # --- LongMoodState の統合更新（baseline + shock） ---
        # NOTE:
        # - WritePlan の state_updates.long_mood_state は「本文案」として扱う。
        # - 数値（baseline/shock）は event_affect の moment_vad を入力として更新する。
        # - event_affect が無いターンでも、日付が変わった等で本文案があれば本文だけ反映できるようにする。
        if (moment_vad is not None and moment_conf > 0.0) or (baseline_text_candidate is not None and str(baseline_text_candidate).strip()):
            # --- 既存の long_mood_state を読む（単一前提） ---
            st = (
                db.query(State)
                .filter(State.kind == "long_mood_state")
                .filter(State.searchable == 1)
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .first()
            )
            # --- 共通ロジックで baseline+shock を更新する ---
            payload_prev_obj = affect.parse_long_mood_payload(str(st.payload_json or "")) if st is not None else None
            prev_body_text = str(st.body_text or "") if st is not None else None
            prev_confidence = float(st.confidence) if st is not None else None
            prev_last_confirmed_at = int(st.last_confirmed_at) if st is not None else None

            update_result = affect.update_long_mood(
                prev_state_exists=(st is not None),
                prev_payload_obj=payload_prev_obj,
                prev_body_text=prev_body_text,
                prev_confidence=prev_confidence,
                prev_last_confirmed_at=prev_last_confirmed_at,
                event_ts=int(base_ts),
                moment_vad=moment_vad,
                moment_confidence=float(moment_conf),
                moment_note=moment_note,
                baseline_text_candidate=baseline_text_candidate,
            )

            payload_new_json = common_utils.json_dumps(update_result.payload_obj)

            # --- 変更が無い場合は何もしない（revisionノイズを避ける） ---
            skip_upsert = False
            if st is not None:
                payload_prev_json = str(st.payload_json or "").strip()
                same_payload = payload_prev_json == payload_new_json
                same_text = str(st.body_text or "").strip() == str(update_result.body_text or "").strip()
                same_conf = abs(float(st.confidence) - float(update_result.confidence)) < 1e-9
                same_lca = int(st.last_confirmed_at) == int(update_result.last_confirmed_at)
                if same_payload and same_text and same_conf and same_lca:
                    skip_upsert = True

            # --- upsert（単一更新） ---
            if skip_upsert:
                changed_state_id = 0
            elif st is None:
                st2 = State(
                    kind="long_mood_state",
                    body_text=str(update_result.body_text),
                    payload_json=payload_new_json,
                    last_confirmed_at=int(update_result.last_confirmed_at),
                    confidence=float(update_result.confidence),
                    salience=float(0.7),
                    valid_from_ts=None,
                    valid_to_ts=None,
                    created_at=int(base_ts),
                    updated_at=int(now_ts),
                )
                db.add(st2)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(st2.state_id),
                    before=None,
                    after=_state_row_to_json(st2),
                    reason="long_mood_state を更新した（baseline+shock）",
                    evidence_event_ids=[int(event_id)],
                )
                changed_state_id = int(st2.state_id)
            else:
                before = _state_row_to_json(st)
                st.kind = "long_mood_state"
                st.body_text = str(update_result.body_text)
                st.payload_json = payload_new_json
                st.last_confirmed_at = int(update_result.last_confirmed_at)
                st.confidence = float(update_result.confidence)
                # NOTE: salience は固定でよい（長期気分は背景。検索の強さではなく注入で使う）。
                st.salience = float(0.7)
                st.valid_from_ts = None
                st.valid_to_ts = None
                st.updated_at = int(now_ts)
                db.add(st)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(st.state_id),
                    before=before,
                    after=_state_row_to_json(st),
                    reason="long_mood_state を更新した（baseline+shock）",
                    evidence_event_ids=[int(event_id)],
                )
                changed_state_id = int(st.state_id)

            # --- embedding job（long_mood_state も更新で育つので反映しておく） ---
            if int(changed_state_id) > 0:
                db.add(
                    Job(
                        kind="upsert_state_embedding",
                        payload_json=common_utils.json_dumps({"state_id": int(changed_state_id)}),
                        status=int(_JOB_PENDING),
                        run_after=int(now_ts),
                        tries=0,
                        last_error=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                )

        # NOTE:
        # - state_entities は各 state_update の entities を正にして即時反映している。
        # - ここでは何もしない（ループ内で確定した state_id に対して更新済み）。

        # --- state_links リンク生成ジョブ ---
        # NOTE:
        # - state↔state のリンク生成は「同期検索の前」には重すぎるため、非同期ジョブとして回す。
        # - apply_write_plan は「記憶更新の本体」なので、リンク生成の失敗で全体を止めない（try/exceptで握る）。
        # - long_mood_state は対象外（背景であり、リンクで増殖させない）。
        try:
            from cocoro_ghost.config import get_config_store

            cfg = get_config_store().config
            build_enabled = bool(getattr(cfg, "memory_state_links_build_enabled", True))
            target_limit = int(getattr(cfg, "memory_state_links_build_target_state_limit", 3))
            target_limit = max(0, int(target_limit))
        except Exception:  # noqa: BLE001
            build_enabled = True
            target_limit = 3

        if build_enabled and updated_state_ids_for_links:
            # --- 対象stateを絞る（直近の更新ほど優先: state_id降順） ---
            # NOTE: 1回のWritePlanで大量のstateが触られても、リンク生成は少数だけにしてコストとノイズを抑える。
            target_state_ids = sorted({int(x) for x in updated_state_ids_for_links if int(x) > 0}, reverse=True)
            if target_limit > 0:
                target_state_ids = target_state_ids[: int(target_limit)]

            if target_state_ids:
                db.add(
                    Job(
                        kind="build_state_links",
                        payload_json=common_utils.json_dumps(
                            {
                                "event_id": int(event_id),
                                "state_ids": [int(x) for x in target_state_ids],
                            }
                        ),
                        status=int(_JOB_PENDING),
                        # NOTE: 直後は embedding 更新ジョブも積まれるので、軽く遅延して混雑を避ける。
                        run_after=int(now_ts) + 3,
                        tries=0,
                        last_error=None,
                        created_at=int(now_ts),
                        updated_at=int(now_ts),
                    )
                )

        # --- Nターンごとの整理ジョブ（chatターン回数ベース） ---
        # NOTE:
        # - 併用はしない（定期/閾値は採用しない）
        # - pending/running がある場合は二重投入しない
        if not _has_pending_or_running_job(db, kind="tidy_memory"):
            last_watermark_event_id = _last_tidy_watermark_event_id(db)
            interval_turns = (
                int(_TIDY_CHAT_TURNS_INTERVAL_FIRST)
                if int(last_watermark_event_id) <= 0
                else int(_TIDY_CHAT_TURNS_INTERVAL)
            )
            chat_turns_since = (
                db.query(func.count(Event.event_id))
                .filter(Event.source == "chat")
                .filter(Event.event_id > int(last_watermark_event_id))
                .scalar()
            )
            chat_turns_since_i = int(chat_turns_since or 0)
            if chat_turns_since_i >= int(interval_turns):
                max_chat_event_id = (
                    db.query(func.max(Event.event_id))
                    .filter(Event.source == "chat")
                    .scalar()
                )
                watermark_event_id = int(max_chat_event_id or 0)
                if watermark_event_id > 0:
                    _enqueue_tidy_memory_job(
                        db=db,
                        now_ts=int(now_ts),
                        run_after=int(now_ts) + 5,
                        reason="chat_turn_interval",
                        watermark_event_id=int(watermark_event_id),
                    )


def _handle_upsert_event_assistant_summary(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """events.assistant_text の要約を生成し、event_assistant_summaries へ upsert する。"""

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")

    # --- event を読む（必要な材料だけ） ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return

        # --- 本文が無いなら要約しない ---
        assistant_text = str(ev.assistant_text or "").strip()
        if not assistant_text:
            return

        # --- 既に最新の要約があるならスキップ ---
        existing = db.query(EventAssistantSummary).filter(EventAssistantSummary.event_id == int(event_id)).one_or_none()
        if existing is not None:
            if int(existing.event_updated_at) == int(ev.updated_at) and str(existing.summary_text or "").strip():
                return

        input_text = _build_event_assistant_summary_input(ev)
        event_updated_at = int(ev.updated_at)

    if not input_text:
        return

    # --- LLMで要約（JSONのみ） ---
    resp = llm_client.generate_json_response(
        system_prompt=prompt_builders.event_assistant_summary_system_prompt(),
        input_text=input_text,
        purpose=LlmRequestPurpose.ASYNC_EVENT_ASSISTANT_SUMMARY,
        max_tokens=300,
    )
    obj = common_utils.parse_first_json_object_or_none(common_utils.first_choice_content(resp))
    summary = str((obj or {}).get("summary") or "").strip()
    summary = common_utils.strip_face_tags(summary)
    summary = " ".join(summary.replace("\r", " ").replace("\n", " ").split()).strip()

    # --- 空/異常は保存しない（ノイズを増やさない） ---
    if not summary:
        return
    if len(summary) > 280:
        summary = summary[:280].rstrip()

    # --- DBへ upsert（event_id 1:1） ---
    now_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.execute(
            text(
                """
                INSERT INTO event_assistant_summaries(event_id, summary_text, event_updated_at, created_at, updated_at)
                VALUES (:event_id, :summary_text, :event_updated_at, :created_at, :updated_at)
                ON CONFLICT(event_id) DO UPDATE SET
                    summary_text=excluded.summary_text,
                    event_updated_at=excluded.event_updated_at,
                    updated_at=excluded.updated_at
                """
            ),
            {
                "event_id": int(event_id),
                "summary_text": str(summary),
                "event_updated_at": int(event_updated_at),
                "created_at": int(now_ts),
                "updated_at": int(now_ts),
            },
        )


def _handle_build_state_links(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """
    state↔state のリンク（state_links）を生成して保存する。

    目的:
        - state は「育つノート」なので、関連/派生/矛盾/補足のリンクを少数・高品質で残す。
        - 同期検索では `state_link_expand` で辿って候補を増やし、取りこぼしを減らす。

    方針:
        - 候補収集: ベクトル近傍（state）で k 件だけ拾い、距離で足切りする。
        - 判定: LLMで「リンクする/しない + label + confidence」を返させる。
        - 保存: Unique(from,to,label) で upsert し、必ず revisions を残す。
        - 失敗: このジョブだけが失敗する（WritePlan適用は止めない）。
    """

    # --- payload を読む ---
    event_id = int(payload.get("event_id") or 0)
    state_ids_raw = payload.get("state_ids")
    if event_id <= 0:
        raise RuntimeError("payload.event_id is required")
    if not isinstance(state_ids_raw, list):
        raise RuntimeError("payload.state_ids is required (list[int])")

    state_ids = [int(x) for x in state_ids_raw if isinstance(x, (int, float)) and int(x) > 0]
    state_ids = sorted({int(x) for x in state_ids if int(x) > 0}, reverse=True)
    if not state_ids:
        return

    # --- 設定（TOML）を読む ---
    # NOTE: 初期化順や例外の影響を避けるため、取れない場合は安全寄りの既定値へフォールバックする。
    try:
        from cocoro_ghost.config import get_config_store

        cfg = get_config_store().config
        enabled = bool(getattr(cfg, "memory_state_links_build_enabled", True))
        candidate_k = int(getattr(cfg, "memory_state_links_build_candidate_k", 24))
        max_links_per_state = int(getattr(cfg, "memory_state_links_build_max_links_per_state", 6))
        min_conf = float(getattr(cfg, "memory_state_links_build_min_confidence", 0.65))
        max_distance = float(getattr(cfg, "memory_state_links_build_max_distance", 0.35))
    except Exception:  # noqa: BLE001
        enabled = True
        candidate_k = 24
        max_links_per_state = 6
        min_conf = 0.65
        max_distance = 0.35

    if not enabled:
        return

    candidate_k = max(1, min(200, int(candidate_k)))
    max_links_per_state = max(0, min(20, int(max_links_per_state)))
    min_conf = max(0.0, min(1.0, float(min_conf)))
    max_distance = max(0.0, float(max_distance))

    now_ts = _now_utc_ts()

    # --- base_state ごとにリンク生成 ---
    # NOTE:
    # - ここは「少数・高品質」が目的なので、1stateあたり max_links_per_state を厳守する。
    # - long_mood_state は背景であり、リンク対象から除外する。
    #
    # NOTE:
    # - SQLAlchemy の ORM オブジェクトはセッション外に持ち出すと expired/detached になりやすい。
    # - ここはジョブであり安定性を最優先し、DBから読むものは「スナップショット(dict)」に変換して扱う。
    def _payload_preview(payload_json: str) -> str:
        s = str(payload_json or "").strip()
        if not s:
            return ""
        if len(s) > 600:
            return s[:600] + "…"
        return s

    for base_state_id in state_ids:
        if max_links_per_state <= 0:
            continue

        # --- base_state をロードしてスナップショット化（セッション外でORMを触らない） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            base = db.query(State).filter(State.searchable == 1).filter(State.state_id == int(base_state_id)).one_or_none()
            if base is None:
                continue
            if str(getattr(base, "kind", "")) == "long_mood_state":
                continue

            base_text_for_emb = _build_state_embedding_text(base)
            if not base_text_for_emb:
                continue

            base_snapshot = {
                "state_id": int(base.state_id),
                "kind": str(base.kind),
                "body_text": str(base.body_text),
                "payload_preview": _payload_preview(str(base.payload_json)),
                "valid_from_ts": (int(base.valid_from_ts) if base.valid_from_ts is not None else None),
                "valid_to_ts": (int(base.valid_to_ts) if base.valid_to_ts is not None else None),
                "last_confirmed_at": int(base.last_confirmed_at),
            }

        # --- 候補（candidate_states）をベクトル近傍から集める ---
        # NOTE:
        # - base_state 自体の vec_items が未更新でも検索できるよう、クエリ埋め込みは都度生成する。
        # - active_only=True で「現役のstate」を優先し、ノイズを抑える。
        q_emb = llm_client.generate_embedding([base_text_for_emb], purpose=LlmRequestPurpose.ASYNC_STATE_LINKS)[0]

        # --- candidate_states をロードしてスナップショット化 ---
        candidate_snapshots: list[dict[str, Any]] = []
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rows = search_similar_item_ids(
                db,
                query_embedding=q_emb,
                k=int(candidate_k),
                kind=int(vector_index.VEC_KIND_STATE),
                rank_day_range=None,
                active_only=True,
            )

            # --- vec_items -> state_id へ変換し、距離で足切り ---
            candidate_state_ids: list[int] = []
            for r in rows:
                if not r or r[0] is None:
                    continue
                item_id = int(r[0])
                distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                if distance is not None and float(distance) > float(max_distance):
                    continue
                st_id = int(vector_index.vec_entity_id(int(item_id)))
                if st_id <= 0:
                    continue
                if st_id == int(base_snapshot["state_id"]):
                    continue
                if st_id not in candidate_state_ids:
                    candidate_state_ids.append(int(st_id))

            if not candidate_state_ids:
                continue

            # --- candidate_states をロード（検索対象のみ + long_mood_state除外） ---
            cand_states2 = (
                db.query(State)
                .filter(State.searchable == 1)
                .filter(State.state_id.in_([int(x) for x in candidate_state_ids]))
                .filter(State.kind != "long_mood_state")
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .all()
            )
            for s in cand_states2:
                if int(s.state_id) == int(base_snapshot["state_id"]):
                    continue
                candidate_snapshots.append(
                    {
                        "state_id": int(s.state_id),
                        "kind": str(s.kind),
                        "body_text": str(s.body_text),
                        "payload_preview": _payload_preview(str(s.payload_json)),
                        "valid_from_ts": (int(s.valid_from_ts) if s.valid_from_ts is not None else None),
                        "valid_to_ts": (int(s.valid_to_ts) if s.valid_to_ts is not None else None),
                        "last_confirmed_at": int(s.last_confirmed_at),
                    }
                )

        if not candidate_snapshots:
            continue

        # --- LLM入力を構築（JSON文字列） ---
        # NOTE:
        # - ここは Worker 内部用。プロンプトは system 側で固定し、入力は機械的なJSONにする。
        input_obj = {"base_state": dict(base_snapshot), "candidate_states": list(candidate_snapshots)}
        input_text = common_utils.json_dumps(input_obj)

        # --- LLMでリンクを判定（JSONのみ） ---
        resp = llm_client.generate_json_response(
            system_prompt=prompt_builders.state_links_system_prompt(),
            input_text=input_text,
            purpose=LlmRequestPurpose.ASYNC_STATE_LINKS,
            max_tokens=400,
        )
        obj = common_utils.parse_first_json_object_or_none(common_utils.first_choice_content(resp)) or {}
        links_raw = obj.get("links")
        if not isinstance(links_raw, list):
            continue

        # --- 出力を正規化（採用/足切り） ---
        allowed_labels = {"relates_to", "derived_from", "supports", "contradicts"}
        candidate_id_set = {int(s.get("state_id") or 0) for s in candidate_snapshots}

        normalized: list[dict[str, Any]] = []
        for x in links_raw:
            if not isinstance(x, dict):
                continue
            to_state_id = int(x.get("to_state_id") or 0)
            if to_state_id <= 0:
                continue
            if to_state_id == int(base_snapshot["state_id"]):
                continue
            if to_state_id not in candidate_id_set:
                continue

            label = str(x.get("label") or "").strip()
            if label not in allowed_labels:
                continue

            conf = float(x.get("confidence") or 0.0)
            if conf < float(min_conf):
                continue

            why = str(x.get("why") or "").strip()
            if len(why) > 240:
                why = why[:240].rstrip() + "…"

            normalized.append(
                {
                    "to_state_id": int(to_state_id),
                    "label": str(label),
                    "confidence": float(conf),
                    "why": str(why),
                }
            )

        # --- 0件なら保存しない ---
        if not normalized:
            continue

        # --- 上限をかける（max_links_per_state） ---
        normalized.sort(key=lambda d: float(d.get("confidence") or 0.0), reverse=True)
        normalized = normalized[: int(max_links_per_state)]

        # --- DBへ upsert + revision ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- revisions を追加するヘルパ（リンク生成でも説明責任を残す） ---
            def add_revision(
                *,
                entity_type: str,
                entity_id: int,
                before: Any,
                after: Any,
                reason: str,
                evidence_event_ids: list[int],
            ) -> None:
                db.add(
                    Revision(
                        entity_type=str(entity_type),
                        entity_id=int(entity_id),
                        before_json=(common_utils.json_dumps(before) if before is not None else None),
                        after_json=(common_utils.json_dumps(after) if after is not None else None),
                        reason=str(reason or "").strip() or "(no reason)",
                        evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_event_ids if int(x) > 0]),
                        created_at=int(now_ts),
                    )
                )

            # --- evidence_event_ids を安定化 ---
            evidence_ids = sorted({int(event_id)})

            # --- 観測用の件数 ---
            created_links = 0
            updated_links = 0

            def upsert_one_link(
                *,
                from_state_id: int,
                to_state_id: int,
                label: str,
                confidence: float,
                why: str,
                evidence_event_ids_in: list[int],
                reverse: bool,
            ) -> None:
                """state_links を1本 upsert し、revision を残す。"""

                nonlocal created_links, updated_links

                existing = (
                    db.query(StateLink)
                    .filter(StateLink.from_state_id == int(from_state_id))
                    .filter(StateLink.to_state_id == int(to_state_id))
                    .filter(StateLink.label == str(label))
                    .one_or_none()
                )

                # --- reason 文字列を整形 ---
                rev_tag = "（逆向き）" if bool(reverse) else ""
                why2 = str(why or "").strip()
                reason_add = f"state_links を追加した{rev_tag}: {why2}" if why2 else f"state_links を追加した{rev_tag}"
                reason_upd = f"state_links を更新した{rev_tag}: {why2}" if why2 else f"state_links を更新した{rev_tag}"

                if existing is None:
                    link = StateLink(
                        from_state_id=int(from_state_id),
                        to_state_id=int(to_state_id),
                        label=str(label),
                        confidence=float(confidence),
                        evidence_event_ids_json=common_utils.json_dumps([int(x) for x in evidence_event_ids_in if int(x) > 0]),
                        created_at=int(now_ts),
                    )
                    db.add(link)
                    db.flush()
                    add_revision(
                        entity_type="state_links",
                        entity_id=int(link.id),
                        before=None,
                        after=_state_link_row_to_json(link),
                        reason=str(reason_add),
                        evidence_event_ids=[int(x) for x in evidence_event_ids_in if int(x) > 0],
                    )
                    created_links += 1
                    return

                before = _state_link_row_to_json(existing)

                # --- evidence_event_ids をマージ（重複なし） ---
                prev_obj = common_utils.json_loads_maybe(str(existing.evidence_event_ids_json or "[]"))
                prev_ids = (
                    [int(x) for x in prev_obj if isinstance(prev_obj, list) and isinstance(x, (int, float))]
                    if isinstance(prev_obj, list)
                    else []
                )
                merged_ids = sorted({int(x) for x in (prev_ids + list(evidence_event_ids_in)) if int(x) > 0})
                existing.confidence = float(confidence)
                existing.evidence_event_ids_json = common_utils.json_dumps(merged_ids)
                db.add(existing)
                db.flush()
                add_revision(
                    entity_type="state_links",
                    entity_id=int(existing.id),
                    before=before,
                    after=_state_link_row_to_json(existing),
                    reason=str(reason_upd),
                    evidence_event_ids=list(merged_ids),
                )
                updated_links += 1

            # --- 対称関係は「逆向き」も保存する ---
            # NOTE:
            # - 現状の label セットには逆関係（supported_by 等）が無い。
            # - そのため、自動で逆向きを張って良いのは「対称」と見なせる関係だけに限定する。
            # - relates_to / contradicts は対称として扱えるため、両方向に保存する。
            symmetric_labels = {"relates_to", "contradicts"}

            for ln in normalized:
                to_state_id = int(ln["to_state_id"])
                label = str(ln["label"])
                conf = float(ln["confidence"])
                why = str(ln.get("why") or "").strip()

                # --- 正方向（base -> to） ---
                upsert_one_link(
                    from_state_id=int(base_snapshot["state_id"]),
                    to_state_id=int(to_state_id),
                    label=str(label),
                    confidence=float(conf),
                    why=str(why),
                    evidence_event_ids_in=list(evidence_ids),
                    reverse=False,
                )

                # --- 逆方向（to -> base、対称関係のみ） ---
                if str(label) in symmetric_labels:
                    upsert_one_link(
                        from_state_id=int(to_state_id),
                        to_state_id=int(base_snapshot["state_id"]),
                        label=str(label),
                        confidence=float(conf),
                        why=str(why),
                        evidence_event_ids_in=list(evidence_ids),
                        reverse=True,
                    )

        # --- 観測ログ（件数のみ） ---
        logger.info(
            "build_state_links done base_state_id=%s created=%s updated=%s candidates=%s",
            int(base_snapshot["state_id"]),
            int(created_links),
            int(updated_links),
            int(len(candidate_snapshots)),
            extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
        )


def _handle_tidy_memory(*, embedding_preset_id: str, embedding_dimension: int, payload: dict[str, Any]) -> None:
    """記憶整理（削除せず、集約/過去化でノイズを減らす）。"""

    now_ts = _now_utc_ts()
    max_close = int(payload.get("max_close_per_run") or _TIDY_MAX_CLOSE_PER_RUN)
    max_close = max(1, min(int(max_close), 5000))

    # --- 変更件数（観測用） ---
    closed_state_ids: list[int] = []

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        # --- revisions を追加するヘルパ（整理でも説明責任を残す） ---
        def add_revision(*, entity_type: str, entity_id: int, before: Any, after: Any, reason: str) -> None:
            db.add(
                Revision(
                    entity_type=str(entity_type),
                    entity_id=int(entity_id),
                    before_json=(common_utils.json_dumps(before) if before is not None else None),
                    after_json=(common_utils.json_dumps(after) if after is not None else None),
                    reason=str(reason or "").strip() or "(no reason)",
                    evidence_event_ids_json="[]",
                    created_at=int(now_ts),
                )
            )

        # --- 完全一致の重複（kind/body/payload）を過去化する ---
        # NOTE:
        # - 「近い意味」の統合は品質難度が高いので、まずは安全な完全一致だけ整理する
        # - 候補収集は広めを正とするため、ここは「ノイズの上限」を抑える役割
        # - long_mood_state は整理対象から除外する（単一更新で育てるのを正とする）
        active_states = (
            db.query(State)
            .filter(State.valid_to_ts.is_(None))
            .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
            .limit(int(_TIDY_ACTIVE_STATE_FETCH_LIMIT))
            .all()
        )

        seen_keys: set[str] = set()
        for st in active_states:
            if len(closed_state_ids) >= int(max_close):
                break

            # --- long_mood_state は整理対象外 ---
            if str(st.kind) == "long_mood_state":
                continue

            key = "|".join(
                [
                    str(st.kind),
                    _normalize_text_for_dedupe(str(st.body_text)),
                    _canonicalize_json_for_dedupe(str(st.payload_json)),
                ]
            )
            if key in seen_keys:
                before = _state_row_to_json(st)
                # NOTE: 最近性（last_confirmed_at）は弄らない（検索順位を壊さない）
                st.valid_to_ts = int(now_ts)
                st.updated_at = int(now_ts)
                db.add(st)
                db.flush()
                add_revision(
                    entity_type="state",
                    entity_id=int(st.state_id),
                    before=before,
                    after=_state_row_to_json(st),
                    reason="記憶整理: 完全一致の重複stateを過去化した",
                )
                closed_state_ids.append(int(st.state_id))
                continue

            seen_keys.add(key)

        # --- 3) 過去化したstateの埋め込みを更新する（activeフラグを反映） ---
        for state_id in closed_state_ids:
            db.add(
                Job(
                    kind="upsert_state_embedding",
                    payload_json=common_utils.json_dumps({"state_id": int(state_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    # --- 観測ログ（件数のみ） ---
    logger.info(
        "tidy_memory done closed_states=%s",
        int(len(closed_state_ids)),
        extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
    )


def _handle_upsert_state_embedding(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """state の埋め込みを生成し vec_items へ upsert する。"""

    state_id = int(payload.get("state_id") or 0)
    if state_id <= 0:
        raise RuntimeError("payload.state_id is required")

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        st = db.query(State).filter(State.state_id == int(state_id)).one_or_none()
        if st is None:
            return
        # --- 検索対象外（自動分離など）なら、vec_items を消して終了 ---
        if int(getattr(st, "searchable", 1) or 0) != 1:
            item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_STATE), int(state_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_state_embedding_text(st)
        rank_at = int(st.last_confirmed_at)
        payload_obj = common_utils.json_loads_maybe(st.payload_json)
        status = str(payload_obj.get("status") or "").strip()
        active = 0 if status == "done" else 1
        if st.valid_to_ts is not None and int(st.valid_to_ts) < int(_now_utc_ts()) - 86400:
            active = 0

    if not text_in:
        return

    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_STATE_EMBEDDING)[0]
    item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_STATE), int(state_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(vector_index.VEC_KIND_STATE),
            rank_at=int(rank_at),
            active=int(active),
        )


def _handle_upsert_event_affect_embedding(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    llm_client: LlmClient,
    payload: dict[str, Any],
) -> None:
    """event_affects の埋め込みを生成し vec_items へ upsert する。"""

    affect_id = int(payload.get("affect_id") or 0)
    if affect_id <= 0:
        raise RuntimeError("payload.affect_id is required")

    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        aff = db.query(EventAffect).filter(EventAffect.id == int(affect_id)).one_or_none()
        if aff is None:
            return
        # --- 紐づく event が検索対象外なら、vec_items を消して終了 ---
        # NOTE:
        # - event_affect は event の派生であり、event が想起対象外なら affect も想起対象外とする。
        ev = db.query(Event).filter(Event.event_id == int(aff.event_id)).one_or_none()
        if ev is None or int(getattr(ev, "searchable", 1) or 0) != 1:
            item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT_AFFECT), int(affect_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_event_affect_embedding_text(aff)
        rank_at = int(aff.created_at)

    if not text_in:
        return

    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_EVENT_AFFECT_EMBEDDING)[0]
    item_id = vector_index.vec_item_id(int(vector_index.VEC_KIND_EVENT_AFFECT), int(affect_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(vector_index.VEC_KIND_EVENT_AFFECT),
            rank_at=int(rank_at),
            active=1,
        )
