"""記憶・エピソード生成。"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
from datetime import datetime
from typing import List, Optional

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope, search_similar_episodes, upsert_episode_embedding
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost import prompts
from cocoro_ghost.reflection import EpisodeReflection, PersonUpdate, generate_reflection


logger = logging.getLogger(__name__)

_user_locks: dict[str, threading.Lock] = {}


def _get_user_lock(user_id: str) -> threading.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = threading.Lock()
        _user_locks[user_id] = lock
    return lock


def _validate_image_path(path_str: str, allowed_roots: List[pathlib.Path]) -> pathlib.Path:
    path = pathlib.Path(path_str).expanduser().resolve()
    for root in allowed_roots:
        if root in path.parents or path == root:
            return path
    raise ValueError(f"許可されていないパスです: {path_str}")


class MemoryManager:
    def __init__(self, llm_client: LlmClient, config_store: ConfigStore):
        self.llm_client = llm_client
        self.config_store = config_store

    def handle_chat(self, db: Session, request: schemas.ChatRequest, background_tasks: Optional[BackgroundTasks] = None) -> schemas.ChatResponse:
        try:
            logger.info("chat request", extra={"user_id": request.user_id})
            image_summary = None
            if request.image_path:
                try:
                    image_bytes = self._load_image(request.image_path)
                    image_summary = self.llm_client.generate_image_summary([image_bytes])[0]
                except Exception as exc:  # noqa: BLE001
                    logger.warning("画像要約に失敗しました", exc_info=exc)
                    image_summary = "画像要約に失敗しました"

            system_prompt = self.config_store.config.system_prompt
            conversation = [{"role": "user", "content": request.text}]
            if image_summary:
                conversation.append({"role": "system", "content": f"画像要約: {image_summary}"})

            # 類似エピソード検索用の一時埋め込みを生成し、コンテキストを付与
            similar_context = None
            try:
                search_embed = self.llm_client.generate_embedding(
                    ["\n".join(filter(None, [request.text, request.context_hint, image_summary]))]
                )[0]
                similar_rows = search_similar_episodes(db, search_embed, limit=self.config_store.config.similar_episodes_limit)
                if similar_rows:
                    episode_ids = [row.episode_id for row in similar_rows]
                    episodes = (
                        db.query(models.Episode)
                        .filter(models.Episode.id.in_(episode_ids))
                        .all()
                    )
                    parts = []
                    for ep in episodes:
                        parts.append(
                            f"- {ep.occurred_at.isoformat()} {ep.topic_tags or ''} {ep.emotion_label or ''} {ep.user_text or ''} {ep.reply_text or ''}"
                        )
                    similar_context = "\n".join(parts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("類似エピソード検索に失敗しました", exc_info=exc)

            reply_text = self.llm_client.generate_reply(
                system_prompt=system_prompt,
                conversation=conversation if not similar_context else conversation + [{"role": "system", "content": f"最近の関連エピソード:\n{similar_context}"}],
            )

            # 先にエピソードのベースを保存し、後続の重処理はバックグラウンドに回す
            episode_id = self._create_episode(
                db,
                occurred_at=datetime.utcnow(),
                source="chat",
                user_text=request.text,
                reply_text=reply_text,
                reflection=None,
                embedding=None,
                image_summary=image_summary,
            )

            if background_tasks is not None:
                background_tasks.add_task(
                    self._process_chat_background,
                    request.user_id,
                    episode_id,
                    request.text,
                    reply_text,
                    request.context_hint,
                    image_summary,
                )
            else:
                self._process_chat_background(request.user_id, episode_id, request.text, reply_text, request.context_hint, image_summary)

            return schemas.ChatResponse(reply_text=reply_text, episode_id=episode_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("chat processing failed", exc_info=exc)
            return schemas.ChatResponse(reply_text=f"LLMエラー: {exc}", episode_id=-1)

    def handle_notification(self, db: Session, request: schemas.NotificationRequest) -> schemas.NotificationResponse:
        image_summary = None
        if request.image_url:
            image_summary = request.image_url

        system_prompt = prompts.get_notification_prompt()
        base_text = f"{request.source_system}: {request.title}\n{request.body}"
        conversation = [{"role": "user", "content": base_text}]
        speak_text = self.llm_client.generate_reply(system_prompt=system_prompt, conversation=conversation)

        reflection = generate_reflection(
            self.llm_client,
            context_text=f"notification: {base_text}\nreply: {speak_text}",
            image_descriptions=[image_summary] if image_summary else None,
        )

        embedding_input = "\n".join(filter(None, [base_text, speak_text, reflection.reflection_text, image_summary]))
        embedding = self.llm_client.generate_embedding([embedding_input])[0]

        episode_id = self._create_episode(
            db,
            occurred_at=datetime.utcnow(),
            source="notification",
            user_text=base_text,
            reply_text=speak_text,
            reflection=reflection,
            embedding=embedding,
            image_summary=image_summary,
        )
        return schemas.NotificationResponse(speak_text=speak_text, episode_id=episode_id)

    def handle_meta_request(self, db: Session, request: schemas.MetaRequestRequest) -> schemas.MetaRequestResponse:
        system_prompt = prompts.get_notification_prompt()
        base_text = f"instruction: {request.instruction}\npayload: {request.payload_text}"
        conversation = [{"role": "user", "content": base_text}]
        speak_text = self.llm_client.generate_reply(system_prompt=system_prompt, conversation=conversation)

        reflection = generate_reflection(
            self.llm_client,
            context_text=f"meta_request: {base_text}\nreply: {speak_text}",
            image_descriptions=[request.image_url] if request.image_url else None,
        )

        embedding_input = "\n".join(filter(None, [base_text, speak_text, reflection.reflection_text]))
        embedding = self.llm_client.generate_embedding([embedding_input])[0]

        episode_id = self._create_episode(
            db,
            occurred_at=datetime.utcnow(),
            source="meta_request",
            user_text=base_text,
            reply_text=speak_text,
            reflection=reflection,
            embedding=embedding,
            image_summary=request.image_url,
        )
        return schemas.MetaRequestResponse(speak_text=speak_text, episode_id=episode_id)

    def handle_capture(self, db: Session, request: schemas.CaptureRequest) -> schemas.CaptureResponse:
        cfg = self.config_store.config
        text_to_check = (request.context_text or "") + " " + request.image_path
        for keyword in cfg.exclude_keywords:
            if keyword and keyword in text_to_check:
                logger.info("capture skipped due to exclude keyword", extra={"keyword": keyword})
                return schemas.CaptureResponse(episode_id=-1, stored=False)

        image_path = self._validate_capture_path(request)
        image_bytes = self._load_image(str(image_path))
        try:
            image_summary = self.llm_client.generate_image_summary([image_bytes])[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像要約に失敗しました", exc_info=exc)
            image_summary = "画像要約に失敗しました"

        reflection = generate_reflection(
            self.llm_client,
            context_text=request.context_text or "",
            image_descriptions=[image_summary],
        )

        embedding_input = "\n".join(filter(None, [request.context_text, image_summary, reflection.reflection_text]))
        embedding = self.llm_client.generate_embedding([embedding_input])[0]

        source = "desktop_capture" if request.capture_type == "desktop" else "camera_capture"
        episode_id = self._create_episode(
            db,
            occurred_at=datetime.utcnow(),
            source=source,
            user_text=request.context_text,
            reply_text=None,
            reflection=reflection,
            embedding=embedding,
            image_summary=image_summary,
            raw_path=str(image_path),
        )
        return schemas.CaptureResponse(episode_id=episode_id, stored=True)

    def _update_persons(self, db: Session, episode_id: int, persons_data: List[PersonUpdate]) -> None:
        for person_update in persons_data:
            person = (
                db.query(models.Person)
                .filter(models.Person.name == person_update.name)
                .one_or_none()
            )
            if person is None:
                person = models.Person(
                    name=person_update.name,
                    is_user=person_update.is_user,
                    first_seen_at=datetime.utcnow(),
                    mention_count=1,
                )
                db.add(person)
                db.flush()
            else:
                person.mention_count = (person.mention_count or 0) + 1
                person.last_seen_at = datetime.utcnow()

            if person_update.status_update_note:
                person.status_note = person_update.status_update_note
            person.closeness_score = (person.closeness_score or 0.0) + person_update.closeness_delta
            person.worry_score = (person.worry_score or 0.0) + person_update.worry_delta

            link = models.EpisodePerson(episode_id=episode_id, person_id=person.id, role=None)
            db.merge(link)

        db.commit()

    def _create_episode(
        self,
        db: Session,
        *,
        occurred_at: datetime,
        source: str,
        user_text: Optional[str],
        reply_text: Optional[str],
        reflection: Optional[EpisodeReflection] = None,
        embedding: Optional[List[float]] = None,
        image_summary: Optional[str] = None,
        raw_path: Optional[str] = None,
    ) -> int:
        episode = models.Episode(
            occurred_at=occurred_at,
            source=source,
            user_text=user_text,
            reply_text=reply_text,
            image_summary=image_summary,
            reflection_text=reflection.reflection_text if reflection else "",
            reflection_json=reflection.raw_json if reflection else "",
            emotion_label=reflection.emotion_label if reflection else None,
            emotion_intensity=reflection.emotion_intensity if reflection else None,
            topic_tags=",".join(reflection.topic_tags) if reflection else None,
            salience_score=reflection.salience_score if reflection else 0.0,
            episode_comment=reflection.episode_comment if reflection else None,
            episode_embedding=json.dumps(embedding).encode("utf-8") if embedding is not None else None,
            raw_desktop_path=raw_path if source == "desktop_capture" else None,
            raw_camera_path=raw_path if source == "camera_capture" else None,
        )
        db.add(episode)
        db.commit()
        db.refresh(episode)
        if reflection:
            self._update_persons(db, episode.id, reflection.persons)
        if embedding is not None:
            upsert_episode_embedding(db, episode.id, embedding)
        return episode.id

    def _process_chat_background(
        self,
        user_id: str,
        episode_id: int,
        user_text: str,
        reply_text: str,
        context_hint: Optional[str],
        image_summary: Optional[str],
    ) -> None:
        lock = _get_user_lock(user_id)
        with lock, memory_session_scope(self.config_store.memory_id, self.config_store.embedding_dimension) as db:
            reflection = generate_reflection(
                self.llm_client,
                context_text=f"user: {user_text}\nreply: {reply_text}\n{context_hint or ''}",
                image_descriptions=[image_summary] if image_summary else None,
            )

            embedding_input = "\n".join(filter(None, [user_text, reply_text, reflection.reflection_text, image_summary]))
            embedding = self.llm_client.generate_embedding([embedding_input])[0]

            episode = db.query(models.Episode).filter(models.Episode.id == episode_id).one_or_none()
            if episode is None:
                logger.error("episode not found for background processing", extra={"episode_id": episode_id})
                return

            episode.reflection_text = reflection.reflection_text
            episode.reflection_json = reflection.raw_json
            episode.emotion_label = reflection.emotion_label
            episode.emotion_intensity = reflection.emotion_intensity
            episode.topic_tags = ",".join(reflection.topic_tags)
            episode.salience_score = reflection.salience_score
            episode.episode_comment = reflection.episode_comment
            episode.episode_embedding = json.dumps(embedding).encode("utf-8")
            db.add(episode)
            db.commit()
            self._update_persons(db, episode.id, reflection.persons)
            upsert_episode_embedding(db, episode.id, embedding)
            logger.info("chat background updated", extra={"episode_id": episode.id})

    def _validate_capture_path(self, request: schemas.CaptureRequest) -> pathlib.Path:
        root_desktop = pathlib.Path("images/desktop").resolve()
        root_camera = pathlib.Path("images/camera").resolve()
        allowed = [root_desktop, root_camera]
        path = _validate_image_path(request.image_path, allowed)
        if request.capture_type == "desktop" and not (root_desktop in path.parents or path == root_desktop):
            raise ValueError("desktop_capture に無効なパスが指定されました")
        if request.capture_type == "camera" and not (root_camera in path.parents or path == root_camera):
            raise ValueError("camera_capture に無効なパスが指定されました")
        return path

    def _load_image(self, path_str: str) -> bytes:
        path = _validate_image_path(path_str, [pathlib.Path("images/desktop").resolve(), pathlib.Path("images/camera").resolve()])
        return path.read_bytes()
