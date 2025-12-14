"""reflection 生成と検証（Unitベース）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost import prompts


@dataclass
class EpisodeReflection:
    reflection_text: str
    emotion_label: str
    emotion_intensity: float
    topic_tags: List[str]
    salience_score: float
    confidence: float
    raw_json: str


def generate_reflection(
    llm_client: LlmClient,
    *,
    context_text: str,
    image_descriptions: Optional[List[str]] = None,
) -> EpisodeReflection:
    ctx = context_text
    if image_descriptions:
        ctx = "\n".join([ctx, *image_descriptions])

    resp = llm_client.generate_json_response(system_prompt=prompts.get_reflection_prompt(), user_text=ctx)
    raw_text = llm_client.response_content(resp)
    raw_json = raw_text
    data = json.loads(raw_text)

    try:
        topic_tags = data.get("topic_tags") or []
        if not isinstance(topic_tags, list):
            topic_tags = []
        return EpisodeReflection(
            reflection_text=str(data.get("reflection_text") or ""),
            emotion_label=str(data.get("emotion_label") or "neutral"),
            emotion_intensity=float(data.get("emotion_intensity") or 0.0),
            topic_tags=[str(x) for x in topic_tags],
            salience_score=float(data.get("salience_score") or 0.0),
            confidence=float(data.get("confidence") or 0.0),
            raw_json=raw_json,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid reflection fields") from exc

