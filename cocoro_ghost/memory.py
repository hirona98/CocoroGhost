"""記憶・エピソード生成の骨組み。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.reflection import EpisodeReflection, PersonUpdate


logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, llm_client: LlmClient, config_store: ConfigStore):
        self.llm_client = llm_client
        self.config_store = config_store

    def handle_chat(self, db: Session, request: schemas.ChatRequest) -> schemas.ChatResponse:
        try:
            reply_text = self.llm_client.generate_reply(
                system_prompt="",  # 実プロンプトは後続で拡張
                conversation=[{"role": "user", "content": request.text}],
            )
            episode_id = self._create_episode(
                db,
                occurred_at=datetime.utcnow(),
                source="chat",
                user_text=request.text,
                reply_text=reply_text,
            )
            return schemas.ChatResponse(reply_text=reply_text, episode_id=episode_id)
        except Exception as exc:  # noqa: BLE001
            error_message = f"LLMエラー: {exc}"
            logger.error("chat processing failed", exc_info=exc)
            return schemas.ChatResponse(reply_text=error_message, episode_id=-1)

    def handle_notification(self, db: Session, request: schemas.NotificationRequest) -> schemas.NotificationResponse:
        speak_text = f"通知を受信しました: {request.title}"
        episode_id = self._create_episode(
            db,
            occurred_at=datetime.utcnow(),
            source="notification",
            user_text=request.body,
            reply_text=speak_text,
        )
        return schemas.NotificationResponse(speak_text=speak_text, episode_id=episode_id)

    def handle_meta_request(self, db: Session, request: schemas.MetaRequestRequest) -> schemas.MetaRequestResponse:
        speak_text = f"メタ指示を受信しました: {request.instruction}"
        episode_id = self._create_episode(
            db,
            occurred_at=datetime.utcnow(),
            source="meta_request",
            user_text=request.payload_text,
            reply_text=speak_text,
        )
        return schemas.MetaRequestResponse(speak_text=speak_text, episode_id=episode_id)

    def handle_capture(self, db: Session, request: schemas.CaptureRequest) -> schemas.CaptureResponse:
        cfg = self.config_store.config
        text_to_check = (request.context_text or "") + " " + request.image_path
        for keyword in cfg.exclude_keywords:
            if keyword and keyword in text_to_check:
                logger.info("capture skipped due to exclude keyword", extra={"keyword": keyword})
                return schemas.CaptureResponse(episode_id=-1, stored=False)

        episode_id = self._create_episode(
            db,
            occurred_at=datetime.utcnow(),
            source="desktop_capture" if request.capture_type == "desktop" else "camera_capture",
            user_text=request.context_text,
            reply_text=None,
            raw_path=request.image_path,
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
        raw_path: Optional[str] = None,
    ) -> int:
        episode = models.Episode(
            occurred_at=occurred_at,
            source=source,
            user_text=user_text,
            reply_text=reply_text,
            reflection_text=reflection.reflection_text if reflection else None,
            reflection_json=reflection.raw_json if reflection else None,
            emotion_label=reflection.emotion_label if reflection else None,
            emotion_intensity=reflection.emotion_intensity if reflection else None,
            topic_tags=",".join(reflection.topic_tags) if reflection else None,
            salience_score=reflection.salience_score if reflection else None,
            episode_embedding=None if embedding is None else bytes(),
            raw_desktop_path=raw_path if source == "desktop_capture" else None,
            raw_camera_path=raw_path if source == "camera_capture" else None,
        )
        db.add(episode)
        db.commit()
        db.refresh(episode)
        if reflection:
            self._update_persons(db, episode.id, reflection.persons)
        return episode.id
