"""API リクエスト/レスポンスの Pydantic モデル。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    user_id: str = "default"
    text: str
    context_hint: Optional[str] = None
    image_base64: Optional[str] = None  # BASE64エンコードされた画像データ


class ChatResponse(BaseModel):
    reply_text: str
    episode_id: int


class NotificationRequest(BaseModel):
    source_system: str
    title: str
    body: str
    image_base64: Optional[str] = None  # BASE64エンコードされた画像データ


class NotificationResponse(BaseModel):
    speak_text: str
    episode_id: int


class MetaRequestRequest(BaseModel):
    instruction: str
    payload_text: str
    image_base64: Optional[str] = None  # BASE64エンコードされた画像データ


class MetaRequestResponse(BaseModel):
    speak_text: str
    episode_id: int


class CaptureRequest(BaseModel):
    capture_type: str  # "desktop" or "camera"
    image_base64: str  # BASE64エンコードされた画像データ
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


# --- 設定スキーマ ---


class GlobalSettingsResponse(BaseModel):
    """共通設定レスポンス。"""

    exclude_keywords: List[str]
    active_llm_preset_id: Optional[int]
    active_character_preset_id: Optional[int]


class GlobalSettingsUpdateRequest(BaseModel):
    """共通設定更新リクエスト。"""

    exclude_keywords: Optional[List[str]] = None


# LLMプリセット


class LlmPresetResponse(BaseModel):
    """LLMプリセットレスポンス。"""

    id: int
    name: str
    llm_api_key: Optional[str]
    llm_model: str
    llm_base_url: Optional[str]
    reasoning_effort: Optional[str]
    max_turns_window: int
    max_tokens_vision: int
    max_tokens: int
    embedding_model: str
    embedding_api_key: Optional[str]
    embedding_base_url: Optional[str]
    embedding_dimension: int
    image_model: str
    image_model_api_key: Optional[str]
    image_llm_base_url: Optional[str]
    image_timeout_seconds: int
    similar_episodes_limit: int

    class Config:
        from_attributes = True


class LlmPresetCreateRequest(BaseModel):
    """LLMプリセット作成リクエスト。"""

    name: str
    llm_api_key: str
    llm_model: str
    llm_base_url: Optional[str] = None
    reasoning_effort: Optional[str] = None
    max_turns_window: int = 50
    max_tokens_vision: int = 4096
    max_tokens: int = 4096
    embedding_model: str
    embedding_api_key: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_dimension: int
    image_model: str
    image_model_api_key: Optional[str] = None
    image_llm_base_url: Optional[str] = None
    image_timeout_seconds: int = 60
    similar_episodes_limit: int = 5


class LlmPresetUpdateRequest(BaseModel):
    """LLMプリセット更新リクエスト。"""

    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    reasoning_effort: Optional[str] = None
    max_turns_window: Optional[int] = None
    max_tokens_vision: Optional[int] = None
    max_tokens: Optional[int] = None
    embedding_model: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_dimension: Optional[int] = None
    image_model: Optional[str] = None
    image_model_api_key: Optional[str] = None
    image_llm_base_url: Optional[str] = None
    image_timeout_seconds: Optional[int] = None
    similar_episodes_limit: Optional[int] = None


class LlmPresetSummary(BaseModel):
    """LLMプリセット一覧用。"""

    id: int
    name: str
    llm_model: str

    class Config:
        from_attributes = True


class LlmPresetsListResponse(BaseModel):
    """LLMプリセット一覧レスポンス。"""

    presets: List[LlmPresetSummary]
    active_id: Optional[int]


# キャラクタープリセット


class CharacterPresetResponse(BaseModel):
    """キャラクタープリセットレスポンス。"""

    id: int
    name: str
    system_prompt: str
    memory_id: str

    class Config:
        from_attributes = True


class CharacterPresetCreateRequest(BaseModel):
    """キャラクタープリセット作成リクエスト。"""

    name: str
    system_prompt: str
    memory_id: str = "default"


class CharacterPresetUpdateRequest(BaseModel):
    """キャラクタープリセット更新リクエスト。"""

    system_prompt: Optional[str] = None
    memory_id: Optional[str] = None


class CharacterPresetSummary(BaseModel):
    """キャラクタープリセット一覧用。"""

    id: int
    name: str
    memory_id: str

    class Config:
        from_attributes = True


class CharacterPresetsListResponse(BaseModel):
    """キャラクタープリセット一覧レスポンス。"""

    presets: List[CharacterPresetSummary]
    active_id: Optional[int]


# 統合設定レスポンス


class FullSettingsResponse(BaseModel):
    """全設定統合レスポンス。"""

    # 共通設定
    exclude_keywords: List[str]

    # アクティブなLLMプリセット
    llm_preset: Optional[LlmPresetResponse]

    # アクティブなキャラクタープリセット
    character_preset: Optional[CharacterPresetResponse]


class ActivateResponse(BaseModel):
    """プリセットアクティベートレスポンス。"""

    message: str
    restart_required: bool
