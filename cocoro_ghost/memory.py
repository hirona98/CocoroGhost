"""
記憶・チャット処理（新仕様）

このモジュールは CocoroGhost の中心として、以下を担う。

- `/api/chat`（SSE）: 出来事ログ（events）作成 → 検索 → 返答ストリーム → 非同期更新のキック
- `/api/v2/notification`: 通知を出来事ログとして保存し、イベントストリームへ配信
- `/api/v2/meta-request`: 外部要求は保存せず、能動メッセージの「結果」だけを保存（events.source="meta_proactive"）
- `/api/v2/vision/capture-response`: 画像を保存せず、詳細な画像説明テキストを出来事ログとして保存

注意:
- 運用前のため、マイグレーションや旧スキーマ互換は前提にしない。
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Generator, Optional

from fastapi import BackgroundTasks
from sqlalchemy import text

from cocoro_ghost import event_stream, schemas
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.db import search_similar_item_ids
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import Event, EventAffect, EventLink, Job, RetrievalRun, State
from cocoro_ghost import vision_bridge
from cocoro_ghost.time_utils import format_iso8601_local


logger = logging.getLogger(__name__)


_VEC_KIND_EVENT = 1
_VEC_KIND_STATE = 2
_VEC_KIND_EVENT_AFFECT = 3
_VEC_ID_STRIDE = 10_000_000_000

_JOB_PENDING = 0


def _now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""
    return int(time.time())


def _json_dumps(payload: Any) -> str:
    """DB保存向けにJSONを安定した形式でダンプする（日本語保持）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _vec_item_id(kind: int, entity_id: int) -> int:
    """vec_items の item_id を決定する（kind + entity_id の名前空間衝突を避ける）。"""
    return int(kind) * int(_VEC_ID_STRIDE) + int(entity_id)


def _vec_entity_id(item_id: int) -> int:
    """vec_items の item_id から entity_id を復元する。"""
    return int(item_id) % int(_VEC_ID_STRIDE)


def _first_choice_content(resp: Any) -> str:
    """LiteLLMのレスポンスから最初のchoiceのcontentを取り出す。"""
    try:
        return str(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        try:
            return str(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            return ""


def _parse_first_json_object(text: str) -> dict[str, Any] | None:
    """LLM出力から最初のJSONオブジェクトを抽出してdictとして返す。"""
    s = str(text or "").strip()
    if not s:
        return None

    # --- llm_client の内部ユーティリティで抽出/修復する ---
    from cocoro_ghost.llm_client import _extract_first_json_value, _repair_json_like_text  # noqa: PLC0415

    candidate = _extract_first_json_value(s)
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            obj = json.loads(_repair_json_like_text(candidate))
        except Exception:  # noqa: BLE001
            return None
    return obj if isinstance(obj, dict) else None


def _sse(event: str, data: dict) -> str:
    """SSEの1イベントを文字列化する。"""
    return f"event: {event}\n" + f"data: {_json_dumps(data)}\n\n"


class _UserVisibleReplySanitizer:
    """ユーザーに見せる本文から、内部コンテキストの混入を除去する。"""

    def __init__(self) -> None:
        # --- feed()で改行が来るまで保留する末尾（行未確定） ---
        self._pending: str = ""

        # --- 内部ブロックをスキップ中かどうか ---
        self._skip_mode: bool = False

        # --- スキップ開始後に、空行以外を1行でも捨てたか ---
        self._skipped_any_line_in_block: bool = False

    def feed(self, text: str) -> str:
        """差分テキストを取り込み、ユーザーに送ってよいテキストだけ返す。"""

        # --- 空は即返す ---
        if not text:
            return ""

        # --- バッファへ追加 ---
        self._pending += text

        # --- 改行単位で確定処理 ---
        out_parts: list[str] = []
        while True:
            head, sep, tail = self._pending.partition("\n")
            if not sep:
                break
            line = head + sep
            self._pending = tail
            kept = self._process_line(line)
            if kept:
                out_parts.append(kept)
        return "".join(out_parts)

    def flush(self) -> str:
        """末尾（改行が無い行）を確定し、送ってよいテキストだけ返す。"""
        if not self._pending:
            return ""
        tail = self._process_line(self._pending)
        self._pending = ""
        return tail

    def _process_line(self, line: str) -> str:
        """1行分を処理し、送信する場合はそのまま返す。"""
        stripped_line = line.rstrip("\n").rstrip("\r").strip()

        # --- 内部ブロックの終端検出 ---
        if self._skip_mode:
            if stripped_line:
                self._skipped_any_line_in_block = True
                return ""
            if self._skipped_any_line_in_block:
                self._skip_mode = False
                self._skipped_any_line_in_block = False
            return ""

        # --- 内部っぽい行が来たら、その行から次の空行まで捨てる ---
        if self._is_internal_line(stripped_line):
            self._skip_mode = True
            self._skipped_any_line_in_block = False
            return ""

        return line

    def _is_internal_line(self, stripped_line: str) -> bool:
        """内部用の制御行/見出しに見えるかを判定する。"""
        if not stripped_line:
            return False

        # --- 内部コンテキスト開始タグ ---
        if stripped_line == "<<INTERNAL_CONTEXT>>":
            return True

        # --- 明示的な内部見出し ---
        if stripped_line.startswith("<<<") and stripped_line.endswith(">>>"):
            return True

        return False


def _search_plan_system_prompt() -> str:
    """SearchPlan生成用のsystem promptを返す。"""
    return "\n".join(
        [
            "あなたは会話用の記憶検索計画（SearchPlan）を作る。",
            "出力はJSONオブジェクトのみ（前後に説明文やコードフェンスは禁止）。",
            "",
            "必須キー:",
            "- mode: associative_recent | targeted_broad | explicit_about_time",
            "- queries: string[]",
            "- time_hint: {about_year_start, about_year_end, life_stage_hint}",
            "- diversify: {by: string[], per_bucket: number}",
            "- limits: {max_candidates: number, max_selected: number}",
            "",
            "注意:",
            "- 日本語で会話している前提。queries は検索語（短め）でよい。",
            "- 迷ったら associative_recent を選ぶ。",
        ]
    ).strip()


def _selection_system_prompt() -> str:
    """SearchResultPack生成（選別）用のsystem promptを返す。"""
    return "\n".join(
        [
            "あなたは会話のために、候補記憶から必要なものだけを選び、SearchResultPackを作る。",
            "出力はJSONオブジェクトのみ（前後に説明文やコードフェンスは禁止）。",
            "",
            "入力には候補一覧（event/state/event_affect）とユーザー入力が与えられる。",
            "目的: 会話に必要な記憶だけを最大 max_selected 件まで選ぶ（ノイズは捨てる）。",
            "",
            "出力スキーマ（概略）:",
            "{",
            '  "selected": [',
            "    {",
            '      "type": "event|state|event_affect",',
            '      "event_id": 0,',
            '      "state_id": 0,',
            '      "affect_id": 0,',
            '      "why": "短い理由",',
            '      "snippet": "短い抜粋（必要なら）"',
            "    }",
            "  ]",
            "}",
            "",
            "注意:",
            "- 連想モードでは最近性を優先する。",
            "- 目的検索モードでは期間/ライフステージの分散を意識する。",
            "- event_affect（内心）は内部用。本文にそのまま出さない前提で、返答の雰囲気調整に使う。",
        ]
    ).strip()


def _reply_system_prompt(*, persona_text: str, addon_text: str) -> str:
    """返答生成用のsystem promptを組み立てる。"""
    parts: list[str] = []

    # --- 内部コンテキスト露出防止 ---
    parts.append(
        "\n".join(
            [
                "重要: <<INTERNAL_CONTEXT>> で始まるメッセージは内部用。本文に出力しない。",
                "- 内部用JSONや内部見出し（<<<...>>>）を本文に出力しない。",
            ]
        ).strip()
    )

    # --- ペルソナ（ユーザー編集） ---
    pt = str(persona_text or "").strip()
    at = str(addon_text or "").strip()
    if pt or at:
        parts.append("\n".join([x for x in [pt, at] if x]).strip())

    return "\n\n".join([p for p in parts if p]).strip()


@dataclass(frozen=True)
class _CandidateItem:
    """候補アイテム（イベント/状態/感情）。"""

    type: str  # event/state/event_affect
    id: int
    rank_ts: int
    meta: dict[str, Any]
    hit_sources: list[str]


class MemoryManager:
    """記憶操作の窓口（API層から呼ばれる）。"""

    def __init__(self, *, llm_client: LlmClient, config_store: ConfigStore) -> None:
        self.llm_client = llm_client
        self.config_store = config_store

    def stream_chat(self, request: schemas.ChatRequest, background_tasks: BackgroundTasks) -> Generator[str, None, None]:
        """チャットをSSEで返す（出来事ログ作成→検索→ストリーム→非同期更新）。"""

        # --- 設定を取得 ---
        cfg = self.config_store.config
        embedding_preset_id = str(request.embedding_preset_id or cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)

        # --- 入力を正規化 ---
        client_id = str(request.client_id or "").strip()
        input_text = str(request.input_text or "").strip()
        if not input_text:
            yield _sse("error", {"message": "input_text must not be empty", "code": "invalid_request"})
            return

        now_ts = _now_utc_ts()

        # --- eventsを作成（ターン単位） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- 1) events を作る（assistant_text は後で埋める） ---
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=client_id,
                source="chat",
                user_text=input_text,
                assistant_text=None,
                entities_json="[]",
                client_context_json=_json_dumps(request.client_context) if request.client_context is not None else None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

            # --- 2) reply_to（同じclient_idの直前チャット）を軽量に仮置きする ---
            prev = (
                db.query(Event)
                .filter(Event.client_id == client_id)
                .filter(Event.source == "chat")
                .filter(Event.event_id != event_id)
                .order_by(Event.event_id.desc())
                .first()
            )
            if prev is not None:
                # NOTE: 文脈グラフの本更新は非同期で行う。ここでは reply_to だけを即時に張る。
                from cocoro_ghost.memory_models import EventLink  # noqa: PLC0415

                db.add(
                    EventLink(
                        from_event_id=event_id,
                        to_event_id=int(prev.event_id),
                        label="reply_to",
                        confidence=1.0,
                        evidence_event_ids_json="[]",
                        created_at=now_ts,
                    )
                )

        # --- 3) SearchPlan（LLM） ---
        plan_obj: dict[str, Any] = {
            "mode": "associative_recent",
            "queries": [input_text],
            "time_hint": {"about_year_start": None, "about_year_end": None, "life_stage_hint": ""},
            "diversify": {"by": ["life_stage", "about_year_bucket"], "per_bucket": 5},
            "limits": {"max_candidates": 200, "max_selected": 12},
        }
        try:
            resp = self.llm_client.generate_json_response(
                system_prompt=_search_plan_system_prompt(),
                input_text=input_text,
                purpose=LlmRequestPurpose.SEARCH_PLAN,
                max_tokens=500,
            )
            obj = _parse_first_json_object(_first_choice_content(resp))
            if obj is not None:
                plan_obj = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearchPlan generation failed; fallback to default", exc_info=exc)

        # --- 4) 候補収集（取りこぼし防止優先・可能なものは並列） ---
        candidates: list[_CandidateItem] = self._collect_candidates(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            input_text=input_text,
            plan_obj=plan_obj,
        )

        # --- 5) retrieval_runs を記録（plan + candidate統計） ---
        run_id: int | None = None
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rr = RetrievalRun(
                event_id=int(event_id),
                created_at=now_ts,
                plan_json=_json_dumps(plan_obj),
                candidates_json=_json_dumps(
                    {
                        "counts": {
                            "total": len(candidates),
                            "event": sum(1 for c in candidates if c.type == "event"),
                            "state": sum(1 for c in candidates if c.type == "state"),
                            "event_affect": sum(1 for c in candidates if c.type == "event_affect"),
                        },
                        "sources": {f"{c.type}:{c.id}": c.hit_sources for c in candidates},
                    }
                ),
                selected_json="{}",
                timings_json="{}",
                errors_json="{}",
            )
            db.add(rr)
            db.flush()
            run_id = int(rr.run_id)

        # --- 6) 選別（LLM → SearchResultPack） ---
        search_result_pack: dict[str, Any] = {"selected": []}
        try:
            selection_input = _json_dumps(
                {
                    "user_input": input_text,
                    "plan": plan_obj,
                    "candidates": [dict(c.meta) | {"hit_sources": c.hit_sources} for c in candidates],
                }
            )
            resp = self.llm_client.generate_json_response(
                system_prompt=_selection_system_prompt(),
                input_text=selection_input,
                purpose=LlmRequestPurpose.SEARCH_SELECT,
                max_tokens=1500,
            )
            obj = _parse_first_json_object(_first_choice_content(resp))
            if obj is not None:
                search_result_pack = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearchResultPack selection failed; fallback to empty", exc_info=exc)

        # --- 7) retrieval_runs を更新（selected_json） ---
        if run_id is not None:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                db.execute(
                    text("UPDATE retrieval_runs SET selected_json=:v WHERE run_id=:id"),
                    {"v": _json_dumps(search_result_pack), "id": int(run_id)},
                )

        # --- 8) 返答をSSEで生成（SearchResultPackを内部注入） ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        internal_context = _json_dumps({"SearchResultPack": self._inflate_search_result_pack(candidates, search_result_pack)})
        conversation = [
            {"role": "assistant", "content": f"<<INTERNAL_CONTEXT>>\n{internal_context}"},
            {"role": "user", "content": input_text},
        ]

        reply_text = ""
        finish_reason = ""
        sanitizer = _UserVisibleReplySanitizer()
        try:
            resp_stream = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.CONVERSATION,
                stream=True,
            )
            for delta in self.llm_client.stream_delta_chunks(resp_stream):
                if delta.finish_reason:
                    finish_reason = str(delta.finish_reason)
                if not delta.text:
                    continue
                safe = sanitizer.feed(delta.text)
                if safe:
                    reply_text += safe
                    yield _sse("token", {"text": safe})
            tail = sanitizer.flush()
            if tail:
                reply_text += tail
                yield _sse("token", {"text": tail})
        except Exception as exc:  # noqa: BLE001
            logger.error("chat stream failed", exc_info=exc)
            yield _sse("error", {"message": str(exc), "code": "llm_stream_failed"})
            return

        # --- 9) events を更新（assistant_text） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.execute(
                text("UPDATE events SET assistant_text=:t, updated_at=:u WHERE event_id=:id"),
                {"t": reply_text, "u": _now_utc_ts(), "id": int(event_id)},
            )

        # --- 10) 非同期: 埋め込み更新ジョブを積む（次ターンで効く） ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 10.5) 非同期: 記憶更新（WritePlan） ---
        # NOTE:
        # - 返答とは別に「状態/感情/文脈/要約」を育てる。
        # - 体感速度を壊さないため、同期では行わない。
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 11) SSE完了 ---
        yield _sse("done", {"event_id": int(event_id), "reply_text": reply_text, "usage": {"finish_reason": finish_reason}})

    def handle_notification(self, request: schemas.NotificationRequest, *, background_tasks: BackgroundTasks) -> None:
        """通知を受け取り、出来事ログとして保存し、イベントとして配信する。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()

        # --- 入力 ---
        source_system = str(request.source_system or "").strip()
        text_in = str(request.text or "").strip()

        # --- 画像は保存しない（必要なら詳細説明を別イベントで残す） ---
        # NOTE: 今回は通知自体の処理を優先し、画像説明は扱わない。

        # --- LLMで人格メッセージを生成 ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                f"通知: {source_system}",
                text_in,
            ]
        ).strip()

        resp = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.NOTIFICATION,
            stream=False,
        )
        message = _first_choice_content(resp).strip()

        # --- events に保存 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="notification",
                user_text=user_prompt,
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信 ---
        event_stream.publish(
            type="notification",
            event_id=int(event_id),
            data={
                "system_text": f"[{source_system}] {text_in}",
                "message": message,
            },
            target_client_id=None,
            bufferable=True,
        )

        # --- 非同期: 埋め込み更新 ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def handle_meta_request(self, request: schemas.MetaRequestRequest, *, background_tasks: BackgroundTasks) -> None:
        """メタ依頼を受け取り、外部要求は保存せず、能動メッセージの結果だけを保存する。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()

        # --- 入力（永続化しない） ---
        instruction = str(request.instruction or "").strip()
        payload_text = str(request.payload_text or "").strip()

        # --- LLMで能動メッセージを生成（外部要求だとは悟らせない） ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                "あなたは今、自分から話しかけたい話題を思いついた。",
                "次の材料を踏まえて、自然にユーザーへ話しかける短いメッセージを作る。",
                "",
                "材料:",
                instruction,
                payload_text,
            ]
        ).strip()

        resp = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.META_REQUEST,
            stream=False,
        )
        message = _first_choice_content(resp).strip()

        # --- events に保存（sourceで区別） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="meta_proactive",
                user_text=None,
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信 ---
        event_stream.publish(
            type="meta-request",
            event_id=int(event_id),
            data={"message": message},
            target_client_id=None,
            bufferable=True,
        )

        # --- 非同期: 埋め込み更新 ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def handle_vision_capture_response(self, request: schemas.VisionCaptureResponseV2Request) -> None:
        """視覚のcapture-responseを受け取り、待機中の要求へ紐づけ、画像説明を出来事ログへ保存する。"""

        # --- まずは待機中の要求へ紐づける（タイムアウト済みなら何もしない） ---
        ok = vision_bridge.fulfill_capture_response(
            vision_bridge.VisionCaptureResponse(
                request_id=str(request.request_id),
                client_id=str(request.client_id),
                images=list(request.images or []),
                client_context=(request.client_context if request.client_context is not None else None),
                error=(str(request.error).strip() if request.error is not None else None),
            )
        )
        if not ok:
            return

        # --- 画像は保存しない。images があれば詳細説明テキストを生成して events に残す ---
        if not request.images:
            return
        if request.error is not None:
            return

        # --- data URI -> bytes ---
        images_bytes: list[bytes] = []
        for s in list(request.images or []):
            b64 = schemas.data_uri_image_to_base64(s)
            images_bytes.append(base64.b64decode(b64))

        # --- LLMで詳細説明 ---
        descriptions = self.llm_client.generate_image_summary(images_bytes, purpose=LlmRequestPurpose.IMAGE_DETAIL)
        detail_text = "\n\n".join([d.strip() for d in descriptions if str(d or "").strip()]).strip()
        if not detail_text:
            return

        # --- events に保存（画像は保持しない） ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=str(request.client_id),
                source="vision_detail",
                user_text=detail_text,
                assistant_text=None,
                entities_json="[]",
                client_context_json=_json_dumps(request.client_context) if request.client_context is not None else None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # NOTE: vision_detail はUIへ通知する必要がないため events/stream へは配信しない。

        # --- 非同期: 埋め込み更新（次ターン以降で参照できる） ---
        self._enqueue_event_embedding_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def run_reminder_once(self, *, reminder_id: str, target_client_id: str, hhmm: str, content: str) -> None:
        """リマインダーを1件発火し、イベントストリームへ配信する。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()

        # --- LLMで短い文面を生成 ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                "リマインダーを発火する。",
                "必須: 50文字以内で、時刻を含めて自然に言う。",
                "",
                f"時刻: {hhmm}",
                f"内容: {content}",
            ]
        ).strip()

        resp = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.REMINDER,
            stream=False,
        )
        message = _first_choice_content(resp).strip()

        # --- events に保存 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="reminder",
                user_text=str(content),
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信（バッファしない） ---
        event_stream.publish(
            type="reminder",
            event_id=int(event_id),
            data={
                "reminder_id": str(reminder_id),
                "hhmm": str(hhmm),
                "message": message,
            },
            target_client_id=str(target_client_id),
            bufferable=False,
        )

        # --- 非同期: 埋め込み更新 ---
        self._enqueue_event_embedding_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def run_desktop_watch_once(self, *, target_client_id: str) -> str:
        """デスクトップウォッチを1回実行する（結果コードを返す）。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)

        # --- 視覚要求（命令） ---
        resp = vision_bridge.request_capture_and_wait(
            target_client_id=str(target_client_id),
            source="desktop",
            purpose="desktop_watch",
            timeout_seconds=5.0,
            timeout_ms=5000,
        )
        if resp is None:
            return "skipped_idle"
        if vision_bridge.is_capture_skipped_idle(resp.error):
            return "skipped_idle"
        if vision_bridge.is_capture_skipped_excluded_window_title(resp.error):
            return "skipped_excluded_window_title"
        if resp.error is not None:
            return "failed"
        if not resp.images:
            return "failed"

        # --- 画像説明（詳細） ---
        images_bytes: list[bytes] = []
        for s in list(resp.images or []):
            b64 = schemas.data_uri_image_to_base64(s)
            images_bytes.append(base64.b64decode(b64))
        descriptions = self.llm_client.generate_image_summary(images_bytes, purpose=LlmRequestPurpose.IMAGE_SUMMARY_DESKTOP_WATCH)
        detail_text = "\n\n".join([d.strip() for d in descriptions if str(d or "").strip()]).strip()

        # --- LLMで人格コメントを生成 ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                "デスクトップの様子を見て、自然に短いコメントを言う。",
                "画像の詳細説明（内部用）:",
                detail_text,
            ]
        ).strip()
        resp2 = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "assistant", "content": f"<<INTERNAL_CONTEXT>>\n{detail_text}"}, {"role": "user", "content": "コメントを一言で。"}],
            purpose=LlmRequestPurpose.DESKTOP_WATCH,
            stream=False,
        )
        message = _first_choice_content(resp2).strip()

        # --- events に保存（画像は保持しない） ---
        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=str(target_client_id),
                source="desktop_watch",
                user_text=detail_text,
                assistant_text=message,
                entities_json="[]",
                client_context_json=_json_dumps(resp.client_context) if resp.client_context is not None else None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信（リアルタイム性を優先してバッファしない） ---
        event_stream.publish(
            type="desktop_watch",
            event_id=int(event_id),
            data={
                "system_text": "[desktop_watch]",
                "message": message,
            },
            target_client_id=str(target_client_id),
            bufferable=False,
        )

        # --- 埋め込み更新 ---
        self._enqueue_event_embedding_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )
        return "ok"

    def _enqueue_event_embedding_job(self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
        """出来事ログの埋め込み更新ジョブを積む（次ターンで効かせる）。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):
            return

        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="upsert_event_embedding",
                    payload_json=_json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    def _enqueue_write_plan_job(self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
        """記憶更新のための WritePlan 生成ジョブを積む。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):
            return

        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="generate_write_plan",
                    payload_json=_json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    def _collect_candidates(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        event_id: int,
        input_text: str,
        plan_obj: dict[str, Any],
    ) -> list[_CandidateItem]:
        """候補収集（取りこぼし防止優先・可能なものは並列）。"""

        # --- 上限 ---
        limits = plan_obj.get("limits") if isinstance(plan_obj, dict) else None
        max_candidates = 200
        if isinstance(limits, dict) and isinstance(limits.get("max_candidates"), (int, float)):
            max_candidates = int(limits.get("max_candidates") or 200)
        max_candidates = max(20, min(400, max_candidates))

        # --- 並列候補収集（タイムアウトで全体が破綻しない） ---
        # NOTE:
        # - セッションはスレッドセーフではないため、各タスクで個別に開く。
        # - 遅い経路（特に embedding）があっても、全体を止めない。
        sources_by_key: dict[tuple[str, int], set[str]] = {}

        def add_sources(keys: list[tuple[str, int]], label: str) -> None:
            for t, i in keys:
                if not t or int(i) <= 0:
                    continue
                k = (str(t), int(i))
                s = sources_by_key.get(k)
                if s is None:
                    s = set()
                    sources_by_key[k] = s
                s.add(str(label))

        def task_recent_events() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = (
                    db.query(Event.event_id)
                    .order_by(Event.created_at.desc(), Event.event_id.desc())
                    .limit(50)
                    .all()
                )
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_trigram_events() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = db.execute(
                    text(
                        """
                        SELECT rowid AS event_id
                        FROM events_fts
                        WHERE events_fts MATCH :q
                        ORDER BY rowid DESC
                        LIMIT 80
                        """
                    ),
                    {"q": str(input_text)},
                ).fetchall()
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_reply_chain_events() -> list[tuple[str, int]]:
            # --- reply_to の連鎖を辿る（軽量な文脈復元） ---
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                cur = int(event_id)
                for _ in range(20):
                    link = (
                        db.query(EventLink)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link.to_event_id)
                    if prev_id <= 0:
                        break
                    out.append(("event", int(prev_id)))
                    cur = prev_id
            return out

        def task_recent_states() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = (
                    db.query(State.state_id)
                    .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                    .limit(40)
                    .all()
                )
                return [("state", int(r[0])) for r in rows if r and r[0] is not None]

        def task_about_time_events() -> list[tuple[str, int]]:
            # --- about_time / life_stage で拾う（全期間横断向け） ---
            mode = str(plan_obj.get("mode") or "").strip()
            time_hint = plan_obj.get("time_hint") if isinstance(plan_obj.get("time_hint"), dict) else {}
            y0 = time_hint.get("about_year_start")
            y1 = time_hint.get("about_year_end")
            life = str(time_hint.get("life_stage_hint") or "").strip()

            # NOTE: 明示ヒントが無いなら空で返す（ノイズを増やさない）
            if not y0 and not y1 and not life:
                return []

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                q = db.query(Event.event_id)
                if y0 or y1:
                    ys = int(y0) if isinstance(y0, (int, float)) and int(y0) > 0 else None
                    ye = int(y1) if isinstance(y1, (int, float)) and int(y1) > 0 else None
                    if ys is not None and ye is None:
                        ye = ys
                    if ye is not None and ys is None:
                        ys = ye
                    if ys is not None and ye is not None:
                        q = q.filter(Event.about_year_start.isnot(None)).filter(Event.about_year_end.isnot(None))
                        q = q.filter(Event.about_year_start <= int(ye)).filter(Event.about_year_end >= int(ys))
                if life:
                    q = q.filter(Event.life_stage == str(life))
                limit = 120 if mode == "explicit_about_time" else 80
                rows = q.order_by(Event.created_at.desc(), Event.event_id.desc()).limit(int(limit)).all()
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_vector_all() -> list[tuple[str, int]]:
            # --- embedding を作る（重い） ---
            q_emb = self.llm_client.generate_embedding([str(input_text)], purpose=LlmRequestPurpose.RETRIEVAL_QUERY_EMBEDDING)[0]

            # --- mode によって最近性フィルタを使う ---
            mode = str(plan_obj.get("mode") or "").strip()
            rank_range = None
            if mode == "associative_recent":
                today_day = int(_now_utc_ts()) // 86400
                rank_range = (int(today_day) - 90, int(today_day) + 1)

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                out: list[tuple[str, int]] = []

                rows_e = search_similar_item_ids(
                    db,
                    query_embedding=q_emb,
                    k=60,
                    kind=int(_VEC_KIND_EVENT),
                    rank_day_range=rank_range,
                    active_only=True,
                )
                out.extend([("event", _vec_entity_id(int(r[0]))) for r in rows_e if r and r[0] is not None])

                rows_s = search_similar_item_ids(
                    db,
                    query_embedding=q_emb,
                    k=40,
                    kind=int(_VEC_KIND_STATE),
                    rank_day_range=None,
                    active_only=True,
                )
                out.extend([("state", _vec_entity_id(int(r[0]))) for r in rows_s if r and r[0] is not None])

                rows_a = search_similar_item_ids(
                    db,
                    query_embedding=q_emb,
                    k=20,
                    kind=int(_VEC_KIND_EVENT_AFFECT),
                    rank_day_range=None,
                    active_only=True,
                )
                out.extend([("event_affect", _vec_entity_id(int(r[0]))) for r in rows_a if r and r[0] is not None])

                return out

        # --- 並列実行（遅い経路があっても全体が破綻しない） ---
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                "recent_events": ex.submit(task_recent_events),
                "trigram_events": ex.submit(task_trigram_events),
                "reply_chain": ex.submit(task_reply_chain_events),
                "recent_states": ex.submit(task_recent_states),
                "about_time": ex.submit(task_about_time_events),
                "vector_all": ex.submit(task_vector_all),
            }

            timeouts = {
                "recent_events": 0.25,
                "trigram_events": 0.6,
                "reply_chain": 0.2,
                "recent_states": 0.25,
                "about_time": 0.4,
                "vector_all": 2.2,
            }

            for label, fut in futures.items():
                try:
                    keys = fut.result(timeout=float(timeouts.get(label, 0.5)))
                    add_sources([(str(t), int(i)) for (t, i) in keys], label=str(label))
                except Exception:  # noqa: BLE001
                    continue

        # --- レコードをまとめて引く（ORMのDetachedを避けるため、候補のdict化までセッション内で行う） ---
        keys_all = sorted(sources_by_key.keys())
        if not keys_all:
            return []

        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            event_ids = [int(i) for (t, i) in keys_all if t == "event"]
            state_ids = [int(i) for (t, i) in keys_all if t == "state"]
            affect_ids = [int(i) for (t, i) in keys_all if t == "event_affect"]

            events = db.query(Event).filter(Event.event_id.in_(event_ids)).all() if event_ids else []
            states = db.query(State).filter(State.state_id.in_(state_ids)).all() if state_ids else []
            affects = db.query(EventAffect).filter(EventAffect.id.in_(affect_ids)).all() if affect_ids else []

            by_event_id = {int(r.event_id): r for r in events}
            by_state_id = {int(r.state_id): r for r in states}
            by_affect_id = {int(r.id): r for r in affects}

            affect_event_ids = sorted({int(a.event_id) for a in affects if a and a.event_id is not None})
            affect_events = db.query(Event).filter(Event.event_id.in_(affect_event_ids)).all() if affect_event_ids else []
            by_affect_event_id = {int(r.event_id): r for r in affect_events}

            out: list[_CandidateItem] = []
            for t, i in keys_all:
                hit_sources = sorted(list(sources_by_key.get((t, int(i))) or set()))

                if t == "event":
                    r = by_event_id.get(int(i))
                    if r is None:
                        continue
                    out.append(
                        _CandidateItem(
                            type="event",
                            id=int(i),
                            rank_ts=int(r.created_at),
                            meta={
                                "type": "event",
                                "event_id": int(r.event_id),
                                "created_at": int(r.created_at),
                                "created_at_iso_local": format_iso8601_local(int(r.created_at)),
                                "source": str(r.source),
                                "user_text": str(r.user_text or "")[:800],
                                "assistant_text": str(r.assistant_text or "")[:800],
                                "about_time": {
                                    "about_year_start": r.about_year_start,
                                    "about_year_end": r.about_year_end,
                                    "life_stage": r.life_stage,
                                    "confidence": float(r.about_time_confidence),
                                },
                            },
                            hit_sources=hit_sources,
                        )
                    )
                elif t == "state":
                    s = by_state_id.get(int(i))
                    if s is None:
                        continue
                    out.append(
                        _CandidateItem(
                            type="state",
                            id=int(i),
                            rank_ts=int(s.last_confirmed_at),
                            meta={
                                "type": "state",
                                "state_id": int(s.state_id),
                                "kind": str(s.kind),
                                "body_text": str(s.body_text)[:900],
                                "payload_json": str(s.payload_json)[:1200],
                                "last_confirmed_at": int(s.last_confirmed_at),
                                "last_confirmed_at_iso_local": format_iso8601_local(int(s.last_confirmed_at)),
                                "valid_from_ts": s.valid_from_ts,
                                "valid_to_ts": s.valid_to_ts,
                            },
                            hit_sources=hit_sources,
                        )
                    )
                elif t == "event_affect":
                    a = by_affect_id.get(int(i))
                    if a is None:
                        continue
                    ev2 = by_affect_event_id.get(int(a.event_id))
                    event_created_at = int(ev2.created_at) if ev2 is not None else None
                    out.append(
                        _CandidateItem(
                            type="event_affect",
                            id=int(i),
                            rank_ts=int(a.created_at),
                            meta={
                                "type": "event_affect",
                                "affect_id": int(a.id),
                                "event_id": int(a.event_id),
                                "created_at": int(a.created_at),
                                "created_at_iso_local": format_iso8601_local(int(a.created_at)),
                                "event_created_at": event_created_at,
                                "event_created_at_iso_local": format_iso8601_local(event_created_at),
                                "moment_affect_text": str(a.moment_affect_text or "")[:600],
                                "inner_thought_text": (
                                    str(a.inner_thought_text)[:600] if a.inner_thought_text is not None else None
                                ),
                                "vad": {"v": float(a.vad_v), "a": float(a.vad_a), "d": float(a.vad_d)},
                                "confidence": float(a.confidence),
                            },
                            hit_sources=hit_sources,
                        )
                    )

        out = sorted(out, key=lambda x: (len(x.hit_sources), int(x.rank_ts), int(x.id)), reverse=True)
        return out[:max_candidates]

    def _inflate_search_result_pack(self, candidates: list[_CandidateItem], pack: dict[str, Any]) -> dict[str, Any]:
        """選別結果に候補詳細を埋め、返答生成へ渡しやすい形へ整形する。"""

        # --- 入力を正規化 ---
        if not isinstance(pack, dict):
            return {"selected": []}

        selected = pack.get("selected") if isinstance(pack.get("selected"), list) else []

        # --- 候補辞書（type:id -> meta） ---
        by_key: dict[str, dict[str, Any]] = {}
        for c in candidates:
            key = f"{str(c.type)}:{int(c.id)}"
            by_key[key] = dict(c.meta) | {"hit_sources": list(c.hit_sources)}

        out_selected: list[dict[str, Any]] = []
        for s in selected:
            if not isinstance(s, dict):
                continue

            # --- type が無い場合は event_id の有無で推定する（LLMの揺れ吸収） ---
            t = str(s.get("type") or "").strip()
            if not t:
                if int(s.get("event_id") or 0) > 0:
                    t = "event"
                elif int(s.get("state_id") or 0) > 0:
                    t = "state"
                elif int(s.get("affect_id") or 0) > 0:
                    t = "event_affect"
                else:
                    continue

            # --- typeごとにキーを決める ---
            key: str | None = None
            if t == "event":
                eid = int(s.get("event_id") or 0)
                if eid > 0:
                    key = f"event:{eid}"
            elif t == "state":
                sid = int(s.get("state_id") or 0)
                if sid > 0:
                    key = f"state:{sid}"
            elif t == "event_affect":
                aid = int(s.get("affect_id") or 0)
                if aid > 0:
                    key = f"event_affect:{aid}"

            if key is None:
                continue
            item = by_key.get(key)
            if item is None:
                continue

            out_selected.append(
                {
                    "type": str(t),
                    "why": str(s.get("why") or "").strip(),
                    "snippet": str(s.get("snippet") or "").strip(),
                    "item": item,
                }
            )

        return {"selected": out_selected}
