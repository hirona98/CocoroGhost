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

import json
import logging
import time
from typing import Any

from sqlalchemy import func, text

from cocoro_ghost.db import memory_session_scope, upsert_vec_item
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import Event, EventAffect, EventLink, EventThread, Job, Revision, State
from cocoro_ghost.time_utils import format_iso8601_local, parse_iso8601_to_utc_ts


logger = logging.getLogger(__name__)


_VEC_KIND_EVENT = 1
_VEC_KIND_STATE = 2
_VEC_KIND_EVENT_AFFECT = 3
_VEC_ID_STRIDE = 10_000_000_000


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


def _vec_item_id(kind: int, entity_id: int) -> int:
    """vec_items の item_id を決定する（kind + entity_id の衝突を避ける）。"""
    return int(kind) * int(_VEC_ID_STRIDE) + int(entity_id)


def _json_loads_maybe(text_in: str) -> dict[str, Any]:
    """JSON文字列をdictとして読む（失敗時は空dict）。"""
    try:
        obj = json.loads(str(text_in or ""))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _json_dumps(payload: Any) -> str:
    """DB保存向けにJSONを安定した形式でダンプする（日本語保持）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_first_json_object(text_in: str) -> dict[str, Any]:
    """LLM出力から最初のJSONオブジェクトを抽出してdictとして返す。"""
    s = str(text_in or "").strip()
    if not s:
        raise RuntimeError("LLM output is empty")

    # --- llm_client の内部ユーティリティで抽出/修復する ---
    from cocoro_ghost.llm_client import _extract_first_json_value, _repair_json_like_text  # noqa: PLC0415

    candidate = _extract_first_json_value(s)
    if not candidate:
        raise RuntimeError("JSON extract failed")

    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        obj = json.loads(_repair_json_like_text(candidate))

    if not isinstance(obj, dict):
        raise RuntimeError("JSON is not an object")
    return obj


def _clamp_vad(x: Any) -> float:
    """VAD値を -1.0..+1.0 に丸める（壊れた出力でもDBを壊さないため）。"""
    try:
        v = float(x)
    except Exception:  # noqa: BLE001
        return 0.0
    if v < -1.0:
        return -1.0
    if v > 1.0:
        return 1.0
    return v


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

    payload = _json_loads_maybe(str(last.payload_json or ""))
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
            payload_json=_json_dumps(
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
    t = str(aff.moment_affect_text or "").strip()
    if t:
        parts.append(t)

    # --- inner_thought_text ---
    it = str(aff.inner_thought_text or "").strip()
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


def _write_plan_system_prompt() -> str:
    """WritePlan生成用のsystem promptを返す。"""
    return "\n".join(
        [
            "あなたは出来事ログ（event）から、記憶更新のための計画（WritePlan）を作る。",
            "出力はJSONオブジェクトのみ（前後に説明文やコードフェンスは禁止）。",
            "",
            "目的:",
            "- about_time（内容がいつの話か）と entities を推定する",
            "- 状態（state）を更新して「次ターン以降に効く」形へ育てる",
            "- 瞬間的な感情（event_affect）と長期的な気分（long_mood_state）を扱う",
            "- 文脈グラフ（event_threads/event_links）の本更新案を作る",
            "",
            "品質（重要）:",
            "- 入力（event/recent_events/recent_states）に無い事実は作らない。推測する場合は confidence を下げるか、更新しない。",
            "- event.image_summaries / recent_events[*].image_summaries_preview は画像要約（内部用）。画像そのものは無いので、要約に無い細部は断定しない。",
            "- state_updates は必要なものだけ。雑談だけなら空でもよい（ノイズを増やさない）。",
            "- body_text は検索に使う短い本文（会話文の長文や箇条書きは避ける）。",
            "- 矛盾がある場合は上書きせず、並存/期間分割（valid_from_ts/valid_to_ts）や close を使う。",
            "",
            "視点（重要）:",
            "- あなたは人格本人。本文は主観（一人称）で書く",
            "- 自分を三人称で呼ばない",
            "- 例: 「アシスタントはメイド」→「私はメイドとして仕えている」",
            "- 対象: state_updates.body_text / state_updates.reason / event_affect.* / long_mood_state（stateのbody_text）",
            "",
            "観測イベント（重要）:",
            "- event.source が desktop_watch / vision_detail の場合、event.user_text は「ユーザー発話」ではなく「画面の説明テキスト（内部生成）」である。",
            "- この場合、画面内の行為（作業/操作/閲覧/プレイ等）の主体は event.observation.second_person_label（例: マスター）。あなた（人格）は観測者として「見ている/見守っている」。",
            "- 禁止: 画面内の行為を「私が〜している（プレイしている/作業している）」のように自分の行為として書く。",
            "- 例: OK「私はマスターがリズムゲームをプレイしているデスクトップ画面を見ている」 / NG「私はリズムゲームをプレイしている」",
            "",
            "制約:",
            "- VAD（v/a/d）は各軸 -1.0..+1.0",
            "- confidence/salience/about_time_confidence は 0.0..1.0",
            "- どの更新も evidence_event_ids に必ず現在の event_id を含める",
            "- reason は短く具体的に（なぜそう判断したか）",
            "- state_id はDB主キー（整数）か null（文字列IDを作らない）",
            "- op=close/mark_done は state_id 必須（recent_states にあるIDのみ）。op=upsert は state_id=null で新規、state_id>0 で既存更新。",
            "- 日時は ISO 8601（タイムゾーン付き）文字列で出す（例: 2026-01-10T13:24:00+09:00）",
            "",
            "出力スキーマ（キーは識別子なので英語のまま）:",
            "{",
            '  "event_annotations": {',
            '    "about_start_ts": null,',
            '    "about_end_ts": null,',
            '    "about_year_start": null,',
            '    "about_year_end": null,',
            '    "life_stage": "elementary|middle|high|university|work|unknown",',
            '    "about_time_confidence": 0.0,',
            '    "entities": [{"type":"PERSON|ORG|PLACE|THING|TOPIC","name":"string","confidence":0.0}]',
            "  },",
            '  "state_updates": [',
            "    {",
            '      "op": "upsert|close|mark_done",',
            '      "state_id": null,',
            '      "kind": "fact|relation|task|summary|long_mood_state",',
            '      "body_text": "検索に使う短い本文",',
            '      "payload": {},',
            '      "confidence": 0.0,',
            '      "salience": 0.0,',
            '      "valid_from_ts": null,',
            '      "valid_to_ts": null,',
            '      "last_confirmed_at": null,',
            '      "evidence_event_ids": [0],',
            '      "reason": "string"',
            "    }",
            "  ],",
            '  "event_affect": {',
            '    "moment_affect_text": "string",',
            '    "moment_affect_score_vad": {"v": 0.0, "a": 0.0, "d": 0.0},',
            '    "moment_affect_confidence": 0.0,',
            '    "inner_thought_text": null',
            "  },",
            '  "context_updates": {',
            '    "threads": [{"thread_key":"string","confidence":0.0}],',
            '    "links": [{"to_event_id":0,"label":"reply_to|same_topic|caused_by|continuation","confidence":0.0}]',
            "  }",
            "}",
        ]
    ).strip()


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
    stop_event,
) -> None:
    """jobs を監視して実行し続ける（内蔵Worker用エントリポイント）。"""

    _ = periodic_interval_seconds  # cron無し定期は採用しない（ターン回数ベースを正とする）

    # --- ループ ---
    while True:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            return

        ran_any = False
        try:
            ran_any = _tick_once(
                embedding_preset_id=str(embedding_preset_id),
                embedding_dimension=int(embedding_dimension),
                llm_client=llm_client,
                max_jobs=int(max_jobs_per_tick),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("worker tick failed", exc_info=exc)

        # --- ポーリング間隔（仕事があれば詰めて回す） ---
        if not ran_any:
            time.sleep(max(0.05, float(poll_interval_seconds)))


def _tick_once(*, embedding_preset_id: str, embedding_dimension: int, llm_client: LlmClient, max_jobs: int) -> bool:
    """due な jobs を最大 max_jobs 件だけ実行する。"""

    now_ts = _now_utc_ts()
    max_jobs_i = max(1, int(max_jobs))

    # --- due jobs を取る ---
    # NOTE:
    # - memory_session_scope は正常終了時に commit する。
    # - SQLAlchemy は commit で ORM オブジェクトを expire し得るため、
    #   with を抜けた後に rows（Job ORM）へ触ると DetachedInstanceError になり得る。
    # - そのため、外に持ち出すのは「job_id の整数」だけにする。
    job_ids: list[int] = []
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        rows = (
            db.query(Job)
            .filter(Job.status == int(_JOB_PENDING))
            .filter(Job.run_after <= int(now_ts))
            .order_by(Job.run_after.asc(), Job.id.asc())
            .limit(max_jobs_i)
            .all()
        )
        if not rows:
            return False

        # --- 仕事がある時だけ出す（無限ループでのログ氾濫を避ける） ---
        logger.info(
            "worker tick due_jobs=%s",
            len(rows),
            extra={"embedding_preset_id": str(embedding_preset_id), "embedding_dimension": int(embedding_dimension)},
        )

        # --- 実行対象を running にする（同時実行があっても二重処理を減らす） ---
        for j in rows:
            j.status = int(_JOB_RUNNING)
            j.updated_at = int(now_ts)
            db.add(j)
            # --- with の外で使うのは id だけ ---
            job_ids.append(int(j.id))

    # --- running にしたものを順番に処理する ---
    ran_any = False
    for job_id in job_ids:
        ran_any = True
        _run_one_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            llm_client=llm_client,
            job_id=int(job_id),
        )

    return ran_any


def _run_one_job(*, embedding_preset_id: str, embedding_dimension: int, llm_client: LlmClient, job_id: int) -> None:
    """jobs.id を指定して1件実行する（成功/失敗をDBへ反映）。"""

    now_ts = _now_utc_ts()

    # --- job を読む ---
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        job = db.query(Job).filter(Job.id == int(job_id)).one_or_none()
        if job is None:
            return

        kind = str(job.kind or "").strip()
        payload = _json_loads_maybe(job.payload_json)

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
        elif kind == "tidy_memory":
            _handle_tidy_memory(
                embedding_preset_id=embedding_preset_id,
                embedding_dimension=embedding_dimension,
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
            item_id = _vec_item_id(int(_VEC_KIND_EVENT), int(event_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_event_embedding_text(ev)
        rank_at = int(ev.created_at)

    if not text_in:
        return

    # --- embedding を作る ---
    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_EVENT_EMBEDDING)[0]

    # --- vec_items へ書く（kind+entity_idの衝突を避ける） ---
    item_id = _vec_item_id(int(_VEC_KIND_EVENT), int(event_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(_VEC_KIND_EVENT),
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
            # NOTE:
            # - 二人称呼称はペルソナ本文からパースせず、設定（persona preset）で明示する。
            # - 初期化順や例外の影響を避けるため、取れない場合は最小の既定値へフォールバックする。
            second_person_label = "あなた"
            try:
                from cocoro_ghost.config import get_config_store

                cfg = get_config_store().config
                second_person_label = str(getattr(cfg, "second_person_label", "") or "").strip() or "あなた"
            except Exception:  # noqa: BLE001
                second_person_label = "あなた"
            observation_meta = {
                "observer": "self",
                "second_person_label": second_person_label,
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
                "user_text": str(x.user_text or "")[:600],
                "assistant_text": str(x.assistant_text or "")[:600],
                "image_summaries_preview": (
                    "\n".join(_image_summaries_list(getattr(x, "image_summaries_json", None)))[:600] or None
                ),
            }
            for x in recent_events
        ]
        recent_state_snapshots = [
            {
                "state_id": int(s.state_id),
                "kind": str(s.kind),
                "body_text": str(s.body_text)[:600],
                "payload_json": str(s.payload_json)[:800],
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
        system_prompt=_write_plan_system_prompt(),
        input_text=_json_dumps(input_obj),
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
    plan_obj = _parse_first_json_object(content)

    # --- apply_write_plan を投入する（planはpayloadに入れて監査できるようにする） ---
    now_ts = _now_utc_ts()
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        db.add(
            Job(
                kind="apply_write_plan",
                payload_json=_json_dumps({"event_id": int(event_id), "write_plan": plan_obj}),
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
        ev = db.query(Event).filter(Event.event_id == int(event_id)).one_or_none()
        if ev is None:
            return
        base_ts = int(ev.created_at)

        # --- events 注釈更新（about_time/entities） ---
        ann = plan.get("event_annotations") if isinstance(plan, dict) else None
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
            entities = ann.get("entities") if isinstance(ann.get("entities"), list) else []
            ev.entities_json = _json_dumps(entities)
        ev.updated_at = int(now_ts)
        db.add(ev)

        # --- revisions を追加するヘルパ ---
        def add_revision(*, entity_type: str, entity_id: int, before: Any, after: Any, reason: str, evidence_event_ids: list[int]) -> None:
            db.add(
                Revision(
                    entity_type=str(entity_type),
                    entity_id=int(entity_id),
                    before_json=(_json_dumps(before) if before is not None else None),
                    after_json=(_json_dumps(after) if after is not None else None),
                    reason=str(reason or "").strip() or "(no reason)",
                    evidence_event_ids_json=_json_dumps([int(x) for x in evidence_event_ids if int(x) > 0]),
                    created_at=int(now_ts),
                )
            )

        # --- event_affect（瞬間的な感情/内心） ---
        ea = plan.get("event_affect") if isinstance(plan, dict) else None
        if isinstance(ea, dict):
            moment_text = str(ea.get("moment_affect_text") or "").strip()
            if moment_text:
                score = ea.get("moment_affect_score_vad") if isinstance(ea.get("moment_affect_score_vad"), dict) else {}
                conf = float(ea.get("moment_affect_confidence") or 0.0)
                inner = ea.get("inner_thought_text")
                inner_text = str(inner).strip() if inner is not None and str(inner).strip() else None

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
                        moment_affect_labels_json="[]",
                        inner_thought_text=inner_text,
                        vad_v=_clamp_vad(score.get("v")),
                        vad_a=_clamp_vad(score.get("a")),
                        vad_d=_clamp_vad(score.get("d")),
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
                    existing.inner_thought_text = inner_text
                    existing.vad_v = _clamp_vad(score.get("v"))
                    existing.vad_a = _clamp_vad(score.get("a"))
                    existing.vad_d = _clamp_vad(score.get("d"))
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
                        payload_json=_json_dumps({"affect_id": int(affect_id)}),
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
                        evidence_event_ids_json=_json_dumps([int(event_id)]),
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
                    existing.evidence_event_ids_json = _json_dumps([int(event_id)])
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
                        evidence_event_ids_json=_json_dumps([int(event_id)]),
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
                    existing.evidence_event_ids_json = _json_dumps([int(event_id)])
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
        su = plan.get("state_updates") if isinstance(plan, dict) else None
        if isinstance(su, list):
            for u in su:
                if not isinstance(u, dict):
                    continue

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
                            body_text = _json_dumps(payload_obj)[:2000]
                        st = State(
                            kind=str(kind),
                            body_text=str(body_text),
                            payload_json=_json_dumps(payload_obj),
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
                        existing.payload_json = _json_dumps(payload_obj)
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

                    # --- state embedding job ---
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=_json_dumps({"state_id": int(changed_state_id)}),
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
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=_json_dumps({"state_id": int(st.state_id)}),
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
                    pj = _json_loads_maybe(st.payload_json)
                    pj["status"] = "done"
                    st.payload_json = _json_dumps(pj)
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
                    db.add(
                        Job(
                            kind="upsert_state_embedding",
                            payload_json=_json_dumps({"state_id": int(st.state_id)}),
                            status=int(_JOB_PENDING),
                            run_after=int(now_ts),
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
                    before_json=(_json_dumps(before) if before is not None else None),
                    after_json=(_json_dumps(after) if after is not None else None),
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
                    payload_json=_json_dumps({"state_id": int(state_id)}),
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
            item_id = _vec_item_id(int(_VEC_KIND_STATE), int(state_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_state_embedding_text(st)
        rank_at = int(st.last_confirmed_at)
        payload_obj = _json_loads_maybe(st.payload_json)
        status = str(payload_obj.get("status") or "").strip()
        active = 0 if status == "done" else 1
        if st.valid_to_ts is not None and int(st.valid_to_ts) < int(_now_utc_ts()) - 86400:
            active = 0

    if not text_in:
        return

    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_STATE_EMBEDDING)[0]
    item_id = _vec_item_id(int(_VEC_KIND_STATE), int(state_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(_VEC_KIND_STATE),
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
            item_id = _vec_item_id(int(_VEC_KIND_EVENT_AFFECT), int(affect_id))
            db.execute(text("DELETE FROM vec_items WHERE item_id=:item_id"), {"item_id": int(item_id)})
            return
        text_in = _build_event_affect_embedding_text(aff)
        rank_at = int(aff.created_at)

    if not text_in:
        return

    emb = llm_client.generate_embedding([text_in], purpose=LlmRequestPurpose.ASYNC_EVENT_AFFECT_EMBEDDING)[0]
    item_id = _vec_item_id(int(_VEC_KIND_EVENT_AFFECT), int(affect_id))
    with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
        upsert_vec_item(
            db,
            item_id=int(item_id),
            embedding=emb,
            kind=int(_VEC_KIND_EVENT_AFFECT),
            rank_at=int(rank_at),
            active=1,
        )
