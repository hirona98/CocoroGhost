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


class SettingsFullResponse(BaseModel):
    preset_name: str
    llm_api_key: str
    llm_model: str
    reflection_model: str
    embedding_model: str
    embedding_dimension: int
    image_model: str
    image_timeout_seconds: int
    character_prompt: Optional[str]
    intervention_level: Optional[str]
    exclude_keywords: List[str]
    similar_episodes_limit: int
    max_chat_queue: int


class PresetSummary(BaseModel):
    name: str
    is_active: bool
    created_at: datetime


class PresetsListResponse(BaseModel):
    presets: List[PresetSummary]


class PresetCreateRequest(BaseModel):
    name: str
    llm_api_key: str
    llm_model: str
    reflection_model: str
    embedding_model: str
    embedding_dimension: int
    image_model: str
    image_timeout_seconds: int = 60
    character_prompt: Optional[str] = None
    intervention_level: Optional[str] = None
    exclude_keywords: List[str] = Field(default_factory=list)
    similar_episodes_limit: int = 5
    max_chat_queue: int = 10


class PresetUpdateRequest(BaseModel):
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    reflection_model: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_dimension: Optional[int] = None
    image_model: Optional[str] = None
    image_timeout_seconds: Optional[int] = None
    character_prompt: Optional[str] = None
    intervention_level: Optional[str] = None
    exclude_keywords: Optional[List[str]] = None
    similar_episodes_limit: Optional[int] = None
    max_chat_queue: Optional[int] = None


class PresetActivateResponse(BaseModel):
    message: str
    active_preset: str
    restart_required: bool
