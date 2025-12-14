"""API リクエスト/レスポンスの Pydantic モデル。"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


_DATA_URI_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)


def data_uri_image_to_base64(data_uri: str) -> str:
    m = _DATA_URI_IMAGE_RE.match((data_uri or "").strip())
    if not m:
        raise ValueError("invalid data URI (expected data:image/*;base64,...)")
    b64 = re.sub(r"\s+", "", (m.group(2) or "").strip())
    if not b64:
        raise ValueError("empty base64 payload in data URI")
    try:
        base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid base64 payload in data URI") from exc
    return b64


class ChatRequest(BaseModel):
    memory_id: Optional[str] = None
    user_text: str
    images: List[Dict[str, str]] = Field(default_factory=list)
    client_context: Optional[Dict[str, Any]] = None


class NotificationRequest(BaseModel):
    source_system: str
    text: str
    images: List[Dict[str, str]] = Field(default_factory=list)


class NotificationV1Request(BaseModel):
    from_: str = Field(validation_alias="from")
    message: str = Field(validation_alias="message")
    images: List[str] = Field(default_factory=list, max_length=5)

    @field_validator("images")
    @classmethod
    def _validate_images(cls, v: List[str]) -> List[str]:
        if len(v) > 5:
            raise ValueError("images must contain at most 5 items")
        for item in v:
            data_uri_image_to_base64(item)
        return v


class NotificationResponse(BaseModel):
    unit_id: int


class MetaRequestRequest(BaseModel):
    memory_id: Optional[str] = None
    instruction: str
    payload_text: str
    images: List[Dict[str, str]] = Field(default_factory=list)


class MetaRequestV1Request(BaseModel):
    prompt: str
    images: List[str] = Field(default_factory=list, max_length=5)

    @field_validator("images")
    @classmethod
    def _validate_images(cls, v: List[str]) -> List[str]:
        if len(v) > 5:
            raise ValueError("images must contain at most 5 items")
        for item in v:
            data_uri_image_to_base64(item)
        return v


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


class WeeklySummaryEnqueueRequest(BaseModel):
    week_key: Optional[str] = None


class FullSettingsResponse(BaseModel):
    """全設定統合レスポンス。"""

    # 共通設定
    exclude_keywords: List[str]

    # リマインダー
    reminders_enabled: bool
    reminders: List["ReminderSettings"] = Field(default_factory=list)

    # アクティブなプリセットID
    active_llm_preset_id: Optional[str] = None
    active_embedding_preset_id: Optional[str] = None
    active_persona_preset_id: Optional[str] = None
    active_contract_preset_id: Optional[str] = None

    # アクティブなLLMプリセット
    llm_preset: List["LlmPresetSettings"]

    # アクティブなEmbeddingプリセット
    embedding_preset: List["EmbeddingPresetSettings"]

    # プロンプトプリセット（ユーザー編集対象）
    persona_preset: List["PersonaPresetSettings"]
    contract_preset: List["ContractPresetSettings"]


class ActivateResponse(BaseModel):
    """プリセットアクティベートレスポンス。"""

    message: str
    restart_required: bool


class LlmPresetSettings(BaseModel):
    """設定一覧用LLMプリセット情報。"""

    llm_preset_id: str
    llm_preset_name: str
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


class PersonaPresetSettings(BaseModel):
    """設定一覧用personaプロンプトプリセット情報。"""

    persona_preset_id: str
    persona_preset_name: str
    persona_text: str


class ContractPresetSettings(BaseModel):
    """設定一覧用contractプロンプトプリセット情報。"""

    contract_preset_id: str
    contract_preset_name: str
    contract_text: str


class EmbeddingPresetSettings(BaseModel):
    """設定一覧用Embeddingプリセット情報。"""

    embedding_preset_id: str
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
    active_llm_preset_id: str
    active_embedding_preset_id: str
    active_persona_preset_id: str
    active_contract_preset_id: str
    llm_preset: List[LlmPresetSettings]
    embedding_preset: List[EmbeddingPresetSettings]
    persona_preset: List[PersonaPresetSettings]
    contract_preset: List[ContractPresetSettings]
