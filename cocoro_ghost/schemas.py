"""API リクエスト/レスポンスの Pydantic モデル。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    user_id: str = "default"
    text: str
    context_hint: Optional[str] = None
    image_path: Optional[str] = None


class ChatResponse(BaseModel):
    reply_text: str
    episode_id: int


class NotificationRequest(BaseModel):
    source_system: str
    title: str
    body: str
    image_url: Optional[str] = None


class NotificationResponse(BaseModel):
    speak_text: str
    episode_id: int


class MetaRequestRequest(BaseModel):
    instruction: str
    payload_text: str
    image_url: Optional[str] = None


class MetaRequestResponse(BaseModel):
    speak_text: str
    episode_id: int


class CaptureRequest(BaseModel):
    capture_type: str  # "desktop" or "camera"
    image_path: str
    context_text: Optional[str] = None


class CaptureResponse(BaseModel):
    episode_id: int
    stored: bool


class EpisodeSummary(BaseModel):
    id: int
    occurred_at: datetime
    source: str
    user_text: Optional[str]
    reply_text: Optional[str]
    emotion_label: Optional[str]
    salience_score: float

    class Config:
        from_attributes = True


class SettingsUpdateRequest(BaseModel):
    exclude_keywords: Optional[List[str]] = Field(default=None)
    character_prompt: Optional[str] = Field(default=None)
    intervention_level: Optional[str] = Field(default=None)


class SettingsResponse(BaseModel):
    exclude_keywords: List[str]
    character_prompt: Optional[str]
    intervention_level: Optional[str]
