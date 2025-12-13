"""API リクエスト/レスポンスの Pydantic モデル。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    memory_id: Optional[str] = None
    user_text: str
    images: List[Dict[str, str]] = Field(default_factory=list)
    client_context: Optional[Dict[str, Any]] = None


class NotificationRequest(BaseModel):
    memory_id: Optional[str] = None
    source_system: str
    title: str
    body: str


class NotificationResponse(BaseModel):
    unit_id: int


class MetaRequestRequest(BaseModel):
    memory_id: Optional[str] = None
    instruction: str
    payload_text: str


class MetaRequestResponse(BaseModel):
    unit_id: int


class CaptureRequest(BaseModel):
    capture_type: str  # "desktop" or "camera"
    image_base64: str  # BASE64エンコードされた画像データ
    context_text: Optional[str] = None


class CaptureResponse(BaseModel):
    episode_id: int
    stored: bool


class UnitMeta(BaseModel):
    id: int
    kind: int
    occurred_at: Optional[int] = None
    created_at: int
    updated_at: int
    source: Optional[str] = None
    state: int
    confidence: float
    salience: float
    sensitivity: int
    pin: int
    topic_tags: Optional[str] = None
    emotion_label: Optional[str] = None
    emotion_intensity: Optional[float] = None


class UnitListResponse(BaseModel):
    items: List[UnitMeta]


class UnitDetailResponse(BaseModel):
    unit: UnitMeta
    payload: Dict[str, Any] = Field(default_factory=dict)


class UnitUpdateRequest(BaseModel):
    pin: Optional[int] = None
    sensitivity: Optional[int] = None
    state: Optional[int] = None
    topic_tags: Optional[str] = None
    confidence: Optional[float] = None
    salience: Optional[float] = None


class PersonaUpsertRequest(BaseModel):
    persona_text: str
    set_active: bool = True
    sensitivity: int = 0


class ContractUpsertRequest(BaseModel):
    contract_text: str
    set_active: bool = True
    sensitivity: int = 0


class WeeklySummaryEnqueueRequest(BaseModel):
    week_key: Optional[str] = None


class FullSettingsResponse(BaseModel):
    """全設定統合レスポンス。"""

    # 共通設定
    exclude_keywords: List[str]

    # リマインダー
    reminders_enabled: bool
    reminders: List["ReminderSettings"] = Field(default_factory=list)

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
