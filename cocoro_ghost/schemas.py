"""
API リクエスト/レスポンスの Pydantic モデル

FastAPI エンドポイントで使用するリクエスト/レスポンスのスキーマ定義。
バリデーション、シリアライゼーション、OpenAPI ドキュメント生成に使用される。
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# data URI 形式の画像を検出する正規表現
_DATA_URI_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)


def parse_data_uri_image(data_uri: str) -> tuple[str, str]:
    """
    data URI（data:image/*;base64,...）を解析して検証する。

    Returns:
        (mime, base64_payload)

    無効な形式の場合はValueErrorを発生させる。
    """
    m = _DATA_URI_IMAGE_RE.match((data_uri or "").strip())
    if not m:
        raise ValueError("invalid data URI (expected data:image/*;base64,...)")
    mime = str(m.group(1) or "").strip().lower()
    # 空白を除去してbase64部分を取得
    b64 = re.sub(r"\s+", "", (m.group(2) or "").strip())
    if not b64:
        raise ValueError("empty base64 payload in data URI")
    # base64としてデコード可能か検証
    try:
        base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid base64 payload in data URI") from exc
    return (mime, b64)


def data_uri_image_to_base64(data_uri: str) -> str:
    """
    data URI（data:image/*;base64,...）からbase64部分だけを取り出して検証する。
    無効な形式の場合はValueErrorを発生させる。
    """
    _mime, b64 = parse_data_uri_image(data_uri)
    return b64


def data_uri_image_to_mime(data_uri: str) -> str:
    """
    data URI（data:image/*;base64,...）からMIMEタイプ（例: image/png）だけを取り出して検証する。
    無効な形式の場合はValueErrorを発生させる。
    """
    mime, _b64 = parse_data_uri_image(data_uri)
    return mime


def data_uri_image_to_bytes(data_uri: str) -> bytes:
    """
    data URI（data:image/*;base64,...）をbytesへデコードして返す。

    Returns:
        画像バイナリ（bytes）

    無効な形式の場合はValueErrorを発生させる。
    """
    # --- 形式/base64の妥当性チェックは parse_data_uri_image で行う ---
    _mime, b64 = parse_data_uri_image(data_uri)
    # --- デコード本体（validate=True は上で済んでいる） ---
    return base64.b64decode(b64)


# --- チャット関連 ---


class ChatRequest(BaseModel):
    """
    /chat 用リクエスト。
    ユーザーのメッセージと添付画像を受け付ける。
    """
    model_config = ConfigDict(extra="forbid")

    input_text: str = ""                 # 入力テキスト（省略可。空の場合は画像が必要）
    images: List[str] = Field(default_factory=list, max_length=5)  # data URI形式の画像（最大5枚）
    client_context: Optional[Dict[str, Any]] = None  # クライアント側コンテキスト

    @field_validator("images")
    @classmethod
    def _validate_images_max_items(cls, v: List[str]) -> List[str]:
        """
        画像リストのバリデーション（件数のみ）。

        NOTE:
        - 画像の形式（Data URI）やMIMEの許可判定は、/api/chat の仕様上
          「1枚だけ無視して継続」があるため、ここでは厳密に弾かない。
        """
        if len(v) > 5:
            raise ValueError("images must contain at most 5 items")
        return v


class VisionCaptureResponseV2Request(BaseModel):
    """
    /v2/vision/capture-response 用リクエスト。

    クライアントが取得した画像（data URI）を返す。
    """

    request_id: str
    client_id: str
    images: List[str] = Field(default_factory=list, max_length=5)  # data URI形式の画像（最大5枚）
    client_context: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @model_validator(mode="after")
    def _validate_success_or_error(self) -> "VisionCaptureResponseV2Request":
        """
        画像取得結果の整合性を検証する。

        仕様:
        - 成功: error が null かつ images が1枚以上
        - スキップ/失敗: images が空 かつ error が非空文字列
        """

        # --- 値を正規化（空白だけの error は無効） ---
        images = list(self.images or [])
        err = str(self.error or "").strip()

        # --- 成功: 画像があるなら error は禁止 ---
        if images:
            if err:
                raise ValueError("error must be null when images is non-empty")
            return self

        # --- 失敗/スキップ: 画像が無いなら error を必須 ---
        if not err:
            raise ValueError("error must be provided when images is empty")
        return self

    @field_validator("images")
    @classmethod
    def _validate_images(cls, v: List[str]) -> List[str]:
        """画像リストのバリデーション。"""
        if len(v) > 5:
            raise ValueError("images must contain at most 5 items")
        for item in v:
            data_uri_image_to_base64(item)
        return v

    @field_validator("request_id", "client_id")
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        """必須IDが空でないことを保証する。"""
        s = str(v or "").strip()
        if not s:
            raise ValueError("must not be empty")
        return s


# --- 通知関連 ---


class NotificationRequest(BaseModel):
    """
    /notification 用リクエスト（内部形式）。
    外部システムからの通知をAI人格に伝える。
    """
    client_id: Optional[str] = None     # 宛先クライアントID（省略時はブロードキャスト）
    source_system: str                   # 通知元システム名
    text: str                            # 通知テキスト
    images: List[str] = Field(default_factory=list, max_length=5)  # 添付画像（data URI形式、最大5枚）


class NotificationV2Request(BaseModel):
    """
    /notification/v2 用リクエスト。
    data URI形式の画像を受け付けるバージョン。
    """
    client_id: Optional[str] = None     # 宛先クライアントID（省略時はブロードキャスト）
    source_system: str                   # 通知元システム名
    text: str                            # 通知テキスト
    images: List[str] = Field(default_factory=list, max_length=5)  # data URI形式の画像（最大5枚）

    @field_validator("images")
    @classmethod
    def _validate_images(cls, v: List[str]) -> List[str]:
        """
        画像リストのバリデーション（件数のみ）。

        NOTE:
        - /api/chat と同様、「不正画像はその画像だけ無視して継続」を正とするため、
          ここでは data URI の厳密検証は行わない。
        """
        if len(v) > 5:
            raise ValueError("images must contain at most 5 items")
        return v


# --- メタリクエスト関連 ---


class MetaRequestRequest(BaseModel):
    """
    /meta-request 用リクエスト（内部形式）。
    システムからAI人格への指示を伝える。
    """
    embedding_preset_id: Optional[str] = None
    instruction: str                     # AI人格への指示
    payload_text: str                    # 追加情報テキスト
    images: List[str] = Field(default_factory=list)  # 添付画像（data URI形式、最大5枚）


class MetaRequestV2Request(BaseModel):
    """
    /meta-request/v2 用リクエスト。
    data URI形式の画像を受け付けるバージョン。
    """
    instruction: str                     # AI人格への指示
    payload_text: str = ""               # 追加情報テキスト
    images: List[str] = Field(default_factory=list, max_length=5)  # data URI形式の画像

    @field_validator("images")
    @classmethod
    def _validate_images(cls, v: List[str]) -> List[str]:
        """
        画像リストのバリデーション（件数のみ）。

        NOTE:
        - /api/chat と同様、「不正画像はその画像だけ無視して継続」を正とするため、
          ここでは data URI の厳密検証は行わない。
        """
        if len(v) > 5:
            raise ValueError("images must contain at most 5 items")
        return v


# --- 制御関連 ---


class ControlRequest(BaseModel):
    """
    /control 用リクエスト。

    CocoroGhost プロセス自体の制御コマンドを受け付ける。
    現状は shutdown のみ対応する
    """

    action: str
    reason: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        """action は shutdown のみ許可する。"""
        s = str(v or "").strip()
        if s != "shutdown":
            raise ValueError("action must be 'shutdown'")
        return s


class ControlTimeAdvanceRequest(BaseModel):
    """
    /control/time/advance 用リクエスト。

    domain時刻を指定秒だけ前進させる。
    """

    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(ge=1, le=31_536_000)


class ControlTimeSnapshotResponse(BaseModel):
    """
    /control/time 系APIの共通レスポンス。

    system時刻（実時間）とdomain時刻（テスト時刻）の両方を返す。
    """

    system_now_utc_ts: int
    system_now_iso: Optional[str] = None
    domain_now_utc_ts: int
    domain_now_iso: Optional[str] = None
    domain_offset_seconds: int


class EventStreamRuntimeStats(BaseModel):
    """
    イベントストリームのランタイム統計。

    queue逼迫、ドロップ、送信失敗の観測に使う。
    """

    connected_clients: int
    queue_size: int
    queue_maxsize: int
    dispatcher_running: bool
    enqueued_total: int
    dropped_queue_full_total: int
    send_ok_total: int
    send_error_total: int
    target_not_connected_total: int
    publish_rejected_total: int


class LogStreamRuntimeStats(BaseModel):
    """
    ログストリームのランタイム統計。

    queue逼迫、ドロップ、送信失敗、emit系の失敗観測に使う。
    """

    connected_clients: int
    queue_size: int
    queue_maxsize: int
    buffer_size: int
    buffer_max: int
    dispatcher_running: bool
    enqueued_total: int
    dropped_queue_full_total: int
    send_ok_total: int
    send_error_total: int
    emit_skipped_loop_closed_total: int
    emit_error_total: int


class StreamRuntimeStatsResponse(BaseModel):
    """
    ストリーム全体のランタイム統計レスポンス。

    events と logs の統計を1回で取得する。
    """

    events: EventStreamRuntimeStats
    logs: LogStreamRuntimeStats


class WorkerRuntimeStatsResponse(BaseModel):
    """
    Workerのジョブキュー統計レスポンス。

    jobs テーブルの滞留状態（pending/running/stale）観測に使う。
    """

    pending_count: int
    due_pending_count: int
    running_count: int
    stale_running_count: int
    done_count: int
    failed_count: int
    stale_seconds: int


# --- 設定関連 ---


class FullSettingsResponse(BaseModel):
    """
    全設定統合レスポンス。
    アプリケーションのすべての設定を一括で返す。
    """
    # 記憶機能の有効/無効（UI用）
    memory_enabled: bool

    # 視覚（Vision）: デスクトップウォッチ
    desktop_watch_enabled: bool
    desktop_watch_interval_seconds: int
    desktop_watch_target_client_id: Optional[str] = None

    # アクティブなプリセットID
    active_llm_preset_id: Optional[str] = None
    active_embedding_preset_id: Optional[str] = None
    active_persona_preset_id: Optional[str] = None
    active_addon_preset_id: Optional[str] = None

    # 各種プリセット一覧
    llm_preset: List["LlmPresetSettings"]
    embedding_preset: List["EmbeddingPresetSettings"]
    persona_preset: List["PersonaPresetSettings"]
    addon_preset: List["AddonPresetSettings"] = Field(default_factory=list)


class ActivateResponse(BaseModel):
    """プリセットアクティベートレスポンス。"""
    message: str                         # 結果メッセージ
    restart_required: bool               # 再起動が必要かどうか


class LlmPresetSettings(BaseModel):
    """設定一覧用LLMプリセット情報。"""
    llm_preset_id: str
    llm_preset_name: str
    llm_api_key: str
    llm_model: str
    reasoning_effort: Optional[str] = None
    llm_base_url: Optional[str] = None
    max_turns_window: int
    max_tokens: int
    image_model_api_key: Optional[str] = None
    image_model: str
    image_llm_base_url: Optional[str] = None
    max_tokens_vision: int
    image_timeout_seconds: int


class PersonaPresetSettings(BaseModel):
    """設定一覧用personaプロンプトプリセット情報。"""
    persona_preset_id: str
    persona_preset_name: str
    persona_text: str
    second_person_label: str


class AddonPresetSettings(BaseModel):
    """設定一覧用addon（persona追加オプション）プリセット情報。"""
    addon_preset_id: str
    addon_preset_name: str
    addon_text: str


class EmbeddingPresetSettings(BaseModel):
    """設定一覧用Embeddingプリセット情報。"""
    embedding_preset_id: str
    embedding_preset_name: str
    embedding_model_api_key: Optional[str] = None
    embedding_model: str
    embedding_base_url: Optional[str] = None
    embedding_dimension: int
    similar_episodes_limit: int


class FullSettingsUpdateRequest(BaseModel):
    """
    全設定更新リクエスト。
    すべての設定を一括で更新する際に使用する。
    """
    memory_enabled: bool
    desktop_watch_enabled: bool
    desktop_watch_interval_seconds: int
    desktop_watch_target_client_id: Optional[str] = None
    active_llm_preset_id: str
    active_embedding_preset_id: str
    active_persona_preset_id: str
    active_addon_preset_id: str
    llm_preset: List[LlmPresetSettings]
    embedding_preset: List[EmbeddingPresetSettings]
    persona_preset: List[PersonaPresetSettings]
    addon_preset: List[AddonPresetSettings]


# --- リマインダー（専用API: /api/reminders/*） ---


class RemindersGlobalSettingsResponse(BaseModel):
    """リマインダーのグローバル設定レスポンス。"""

    reminders_enabled: bool


class RemindersGlobalSettingsUpdateRequest(BaseModel):
    """リマインダーのグローバル設定更新リクエスト。"""

    reminders_enabled: bool


class ReminderItem(BaseModel):
    """リマインダー1件（一覧/作成/更新の共通レスポンス）。"""

    id: str
    enabled: bool
    repeat_kind: str  # once|daily|weekly
    content: str

    # --- once ---
    scheduled_at: Optional[str] = None

    # --- daily/weekly ---
    time_of_day: Optional[str] = None  # HH:MM
    weekdays: List[str] = Field(default_factory=list)  # sun..sat（Sun-first）

    # --- state (server-managed) ---
    next_fire_at_utc: Optional[int] = None


class RemindersListResponse(BaseModel):
    """リマインダー一覧レスポンス。"""

    items: List[ReminderItem] = Field(default_factory=list)


class ReminderCreateRequest(BaseModel):
    """リマインダー作成リクエスト。"""

    enabled: bool = True
    repeat_kind: str  # once|daily|weekly
    content: str

    # --- once ---
    scheduled_at: Optional[str] = None

    # --- daily/weekly ---
    time_of_day: Optional[str] = None  # HH:MM
    weekdays: List[str] = Field(default_factory=list)  # weeklyのみ


class ReminderUpdateRequest(BaseModel):
    """リマインダー更新リクエスト（部分更新）。"""

    enabled: Optional[bool] = None
    repeat_kind: Optional[str] = None  # once|daily|weekly
    content: Optional[str] = None

    # --- once ---
    scheduled_at: Optional[str] = None

    # --- daily/weekly ---
    time_of_day: Optional[str] = None  # HH:MM
    weekdays: Optional[List[str]] = None  # weeklyのみ（None=変更なし）
