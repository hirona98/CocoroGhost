"""記憶・エピソード生成（Unitベース）。"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Any, Dict, Generator, List, Optional, Sequence

from fastapi import BackgroundTasks

from cocoro_ghost import schemas
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.scheduler import build_memory_pack, classify_intent
from cocoro_ghost.unit_enums import Sensitivity, UnitKind, UnitState
from cocoro_ghost.unit_models import Job, PayloadEpisode, Unit


logger = logging.getLogger(__name__)

_memory_locks: dict[str, threading.Lock] = {}


def _now_utc_ts() -> int:
    return int(time.time())


def _get_memory_lock(memory_id: str) -> threading.Lock:
    lock = _memory_locks.get(memory_id)
    if lock is None:
        lock = threading.Lock()
        _memory_locks[memory_id] = lock
    return lock


def _decode_base64_image(base64_str: str) -> bytes:
    return base64.b64decode(base64_str)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MemoryManager:
    def __init__(self, llm_client: LlmClient, config_store: ConfigStore):
        self.llm_client = llm_client
        self.config_store = config_store

    def _sse(self, event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {_json_dumps(payload)}\n\n"

    def _summarize_images(self, images: Sequence[Dict[str, str]] | None) -> List[str]:
        if not images:
            return []
        blobs: List[bytes] = []
        for img in images:
            b64 = (img.get("base64") or "").strip()
            if not b64:
                continue
            try:
                blobs.append(_decode_base64_image(b64))
            except Exception:  # noqa: BLE001
                continue
        if not blobs:
            return []
        try:
            return [s.strip() for s in self.llm_client.generate_image_summary(blobs)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像要約に失敗しました", exc_info=exc)
            return ["画像要約に失敗しました"]

    def stream_chat(
        self,
        request: schemas.ChatRequest,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> Generator[str, None, None]:
        cfg = self.config_store.config
        memory_id = request.memory_id or self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        image_summaries = self._summarize_images(request.images)
        intent = classify_intent(llm_client=self.llm_client, user_text=request.user_text)

        try:
            with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
                similar_k = int(cfg.similar_limit_by_kind.get("episode") or 0) if cfg.similar_limit_by_kind else 0
                if similar_k <= 0:
                    similar_k = int(cfg.similar_episodes_limit)
                memory_pack = build_memory_pack(
                    db=db,
                    llm_client=self.llm_client,
                    user_text=request.user_text,
                    image_summaries=image_summaries,
                    client_context=request.client_context,
                    now_ts=now_ts,
                    max_inject_tokens=int(cfg.max_inject_tokens),
                    similar_episode_k=similar_k,
                    intent=intent,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("MemoryPack生成に失敗しました", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "memory_pack_failed"})
            return

        system_prompt = cfg.system_prompt.rstrip() + "\n\n" + memory_pack
        conversation = [{"role": "user", "content": request.user_text}]

        try:
            resp_stream = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("stream chat start failed", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "llm_start_failed"})
            return

        collected: List[str] = []
        try:
            for delta in self.llm_client.stream_delta_chunks(resp_stream):
                collected.append(delta)
                yield self._sse("token", {"text": delta})
        except Exception as exc:  # noqa: BLE001
            logger.error("stream chat failed", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "llm_stream_failed"})
            return

        reply_text = "".join(collected)

        image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None
        context_note = _json_dumps(request.client_context) if request.client_context else None

        try:
            with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
                episode_unit_id = self._create_episode_unit(
                    db,
                    now_ts=now_ts,
                    source="chat",
                    user_text=request.user_text,
                    reply_text=reply_text,
                    image_summary=image_summary_text,
                    context_note=context_note,
                    sensitivity=int(Sensitivity.NORMAL),
                )
                self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=episode_unit_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("episode保存に失敗しました", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "db_write_failed"})
            return

        if background_tasks is not None:
            # Workerが別プロセスで動いている想定だが、開発時に手動で処理したい場合のフックとして残す
            pass

        yield self._sse("done", {"episode_unit_id": episode_unit_id, "reply_text": reply_text, "usage": {}})

    def handle_notification(self, request: schemas.NotificationRequest) -> schemas.NotificationResponse:
        memory_id = request.memory_id or self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        base_text = f"{request.source_system}: {request.title}\n{request.body}"
        context_note = _json_dumps(
            {"source_system": request.source_system, "title": request.title, "body": request.body}
        )
        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source="notification",
                user_text=base_text,
                reply_text=None,
                image_summary=None,
                context_note=context_note,
                sensitivity=int(Sensitivity.NORMAL),
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=unit_id)
        return schemas.NotificationResponse(unit_id=unit_id)

    def handle_meta_request(self, request: schemas.MetaRequestRequest) -> schemas.MetaRequestResponse:
        memory_id = request.memory_id or self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        base_text = f"instruction: {request.instruction}\npayload: {request.payload_text}"
        context_note = _json_dumps({"instruction": request.instruction, "payload_text": request.payload_text})
        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source="meta_request",
                user_text=base_text,
                reply_text=None,
                image_summary=None,
                context_note=context_note,
                sensitivity=int(Sensitivity.NORMAL),
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=unit_id)
        return schemas.MetaRequestResponse(unit_id=unit_id)

    def handle_capture(self, request: schemas.CaptureRequest) -> schemas.CaptureResponse:
        cfg = self.config_store.config
        memory_id = self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        text_to_check = request.context_text or ""
        for keyword in cfg.exclude_keywords:
            if keyword and keyword in text_to_check:
                logger.info("capture skipped due to exclude keyword", extra={"keyword": keyword})
                return schemas.CaptureResponse(episode_id=-1, stored=False)

        image_bytes = _decode_base64_image(request.image_base64)
        try:
            image_summary = self.llm_client.generate_image_summary([image_bytes])[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像要約に失敗しました", exc_info=exc)
            image_summary = "画像要約に失敗しました"

        source = "desktop_capture" if request.capture_type == "desktop" else "camera_capture"
        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source=source,
                user_text=request.context_text,
                reply_text=None,
                image_summary=image_summary,
                context_note=None,
                sensitivity=int(Sensitivity.PRIVATE),
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=unit_id)
        return schemas.CaptureResponse(episode_id=unit_id, stored=True)

    def _create_episode_unit(
        self,
        db,
        *,
        now_ts: int,
        source: str,
        user_text: Optional[str],
        reply_text: Optional[str],
        image_summary: Optional[str],
        context_note: Optional[str],
        sensitivity: int,
    ) -> int:
        unit = Unit(
            kind=int(UnitKind.EPISODE),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source=source,
            state=int(UnitState.RAW),
            confidence=0.5,
            salience=0.0,
            sensitivity=sensitivity,
            pin=0,
            topic_tags=None,
            emotion_label=None,
            emotion_intensity=None,
        )
        db.add(unit)
        db.flush()
        payload = PayloadEpisode(
            unit_id=unit.id,
            user_text=user_text,
            reply_text=reply_text,
            image_summary=image_summary,
            context_note=context_note,
            reflection_json=None,
        )
        db.add(payload)
        db.flush()
        return int(unit.id)

    def _enqueue_default_jobs(self, db, *, now_ts: int, unit_id: int) -> None:
        kinds = [
            "reflect_episode",
            "extract_entities",
            "extract_facts",
            "extract_loops",
            "upsert_embeddings",
            "capsule_refresh",
        ]
        for kind in kinds:
            payload = {"unit_id": unit_id}
            if kind == "capsule_refresh":
                payload = {"limit": 5}
            db.add(
                Job(
                    kind=kind,
                    payload_json=_json_dumps(payload),
                    status=0,
                    run_after=now_ts,
                    tries=0,
                    last_error=None,
                    created_at=now_ts,
                    updated_at=now_ts,
                )
            )
