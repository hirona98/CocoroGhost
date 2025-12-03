"""reflection 生成と検証。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost import prompts


@dataclass
class PersonUpdate:
    name: str
    is_user: bool
    relation_update_note: Optional[str]
    status_update_note: Optional[str]
    closeness_delta: float
    worry_delta: float


@dataclass
class EpisodeReflection:
    reflection_text: str
    emotion_label: str
    emotion_intensity: float
    topic_tags: List[str]
    salience_score: float
    episode_comment: str
    persons: List[PersonUpdate]
    raw_json: str


def _validate_person(raw: dict) -> PersonUpdate:
    try:
        return PersonUpdate(
            name=str(raw["name"]),
            is_user=bool(raw["is_user"]),
            relation_update_note=raw.get("relation_update_note"),
            status_update_note=raw.get("status_update_note"),
            closeness_delta=float(raw.get("closeness_delta", 0.0)),
            worry_delta=float(raw.get("worry_delta", 0.0)),
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid person entry in reflection") from exc


def generate_reflection(llm_client: LlmClient, context_text: str, image_descriptions: Optional[List[str]] = None) -> EpisodeReflection:
    raw = llm_client.generate_reflection(
        system_prompt=prompts.get_reflection_prompt(),
        context_text=context_text,
        image_descriptions=image_descriptions,
    )
    if isinstance(raw, str):
        try:
            raw_json = raw
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:  # noqa: B902
            raise ValueError("reflection JSON parse failed") from exc
    else:
        raw_json = json.dumps(raw, ensure_ascii=False)

    try:
        persons_raw = raw.get("persons", []) or []
        persons = [_validate_person(p) for p in persons_raw]
        return EpisodeReflection(
            reflection_text=str(raw["reflection_text"]),
            emotion_label=str(raw["emotion_label"]),
            emotion_intensity=float(raw["emotion_intensity"]),
            topic_tags=list(raw.get("topic_tags", [])),
            salience_score=float(raw["salience_score"]),
            episode_comment=str(raw.get("episode_comment", "")),
            persons=persons,
            raw_json=raw_json,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid reflection fields") from exc
