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


class NotificationRequest(BaseModel):
    source_system: str
    title: str
    body: str
    image_base64: Optional[str] = None  # BASE64エンコードされた画像データ


class NotificationResponse(BaseModel):
    llm_response: dict
    episode_id: int


class MetaRequestRequest(BaseModel):
    instruction: str
    payload_text: str
    image_base64: Optional[str] = None  # BASE64エンコードされた画像データ


class MetaRequestResponse(BaseModel):
    llm_response: dict
    episode_id: int


class CaptureRequest(BaseModel):
    capture_type: str  # "desktop" or "camera"
    image_base64: str  # BASE64エンコードされた画像データ
    context_text: Optional[str] = None


class CaptureResponse(BaseModel):
    episode_id: int
    stored: bool


class FullSettingsResponse(BaseModel):
    """全設定統合レスポンス。"""

    # 共通設定
    exclude_keywords: List[str]

    # リマインダー
    reminders_enabled: bool
    reminders: List["ReminderSettings"] = []

    # アクティブなLLMプリセット
    llm_preset: List["LlmPresetSettings"]

    # アクティブなEmbeddingプリセット
    embedding_preset: List["EmbeddingPresetSettings"]


class ActivateResponse(BaseModel):
    """プリセットアクティベートレスポンス。"""

    message: str
    restart_required: bool


class LlmPresetSettings(BaseModel):
    """設定一覧用LLMプリセット情報。"""

    llm_preset_id: int
    llm_preset_name: str
    system_prompt: str
    llm_api_key: str
    llm_model: str
    reasoning_effort: Optional[str]
    llm_base_url: Optional[str]
    max_turns_window: int
    max_tokens: int
    image_model_api_key: Optional[str]
    image_model: str
    image_llm_base_url: Optional[str]
    max_tokens_vision: int
    image_timeout_seconds: int


class EmbeddingPresetSettings(BaseModel):
    """設定一覧用Embeddingプリセット情報。"""

    embedding_preset_id: int
    embedding_preset_name: str
    embedding_model_api_key: Optional[str]
    embedding_model: str
    embedding_base_url: Optional[str]
    embedding_dimension: int
    similar_episodes_limit: int


class ReminderSettings(BaseModel):
    """設定一覧用リマインダー情報。"""

    scheduled_at: datetime
    content: str


class ReminderUpsertRequest(BaseModel):
    """リマインダーの追加用（IDは扱わない）。"""

    scheduled_at: datetime
    content: str


class FullSettingsUpdateRequest(BaseModel):
    """全設定更新リクエスト。"""

    exclude_keywords: List[str]
    reminders_enabled: bool
    reminders: List[ReminderUpsertRequest]
    llm_preset: List[LlmPresetSettings]
    embedding_preset: List[EmbeddingPresetSettings]
