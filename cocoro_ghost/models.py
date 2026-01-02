"""
ORM モデル定義

設定DBのテーブル構造を定義するSQLAlchemyモデル。
GlobalSettings（共通設定）、各種プリセット（LLM, Embedding, Persona, Addon）、
リマインダーなどを管理する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cocoro_ghost.db import Base
from cocoro_ghost.defaults import DEFAULT_EXCLUDE_KEYWORDS_JSON


# --- 設定DB用モデル ---

# UUIDの文字列長（ハイフン含む36文字）
_UUID_STR_LEN = 36


def _uuid_str() -> str:
    """UUID文字列を生成するファクトリ関数。"""
    return str(uuid4())


class GlobalSettings(Base):
    """
    共通設定テーブル。
    アプリケーション全体の設定とアクティブなプリセットへの参照を保持する。
    """

    __tablename__ = "global_settings"

    # 主キー（UUID文字列）
    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    # API認証トークン
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 記憶から除外するキーワード（JSON配列形式）
    exclude_keywords: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_EXCLUDE_KEYWORDS_JSON)
    # 記憶機能の有効/無効
    memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # リマインダー機能の有効/無効
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # アクティブなプリセットへの外部キー
    active_llm_preset_id: Mapped[Optional[str]] = mapped_column(String(_UUID_STR_LEN), ForeignKey("llm_presets.id"))
    active_embedding_preset_id: Mapped[Optional[str]] = mapped_column(
        String(_UUID_STR_LEN), ForeignKey("embedding_presets.id")
    )
    active_persona_preset_id: Mapped[Optional[str]] = mapped_column(
        String(_UUID_STR_LEN), ForeignKey("persona_presets.id")
    )
    active_addon_preset_id: Mapped[Optional[str]] = mapped_column(String(_UUID_STR_LEN), ForeignKey("addon_presets.id"))

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # リレーションシップ
    active_llm_preset: Mapped[Optional["LlmPreset"]] = relationship("LlmPreset", foreign_keys=[active_llm_preset_id])
    active_embedding_preset: Mapped[Optional["EmbeddingPreset"]] = relationship(
        "EmbeddingPreset", foreign_keys=[active_embedding_preset_id]
    )
    active_persona_preset: Mapped[Optional["PersonaPreset"]] = relationship(
        "PersonaPreset", foreign_keys=[active_persona_preset_id]
    )
    active_addon_preset: Mapped[Optional["AddonPreset"]] = relationship(
        "AddonPreset", foreign_keys=[active_addon_preset_id]
    )


class LlmPreset(Base):
    """
    LLMプリセットテーブル。
    LLMの接続設定（APIキー、モデル名、パラメータ等）を保持する。
    """

    __tablename__ = "llm_presets"

    # 主キー（UUID文字列）
    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    # プリセット名は重複を許可する（UIで同名作成を許容するため）
    name: Mapped[str] = mapped_column(String, nullable=False)
    # アーカイブフラグ（論理削除用）
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # LLM設定
    llm_api_key: Mapped[str] = mapped_column(String, nullable=False)
    llm_model: Mapped[str] = mapped_column(String, nullable=False)
    llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String)
    max_turns_window: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    max_tokens_vision: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)

    # Image LLM設定（画像認識用の別モデル設定）
    image_model: Mapped[str] = mapped_column(String, nullable=False)
    image_model_api_key: Mapped[Optional[str]] = mapped_column(String)
    image_llm_base_url: Mapped[Optional[str]] = mapped_column(String)
    image_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class PersonaPreset(Base):
    """
    ペルソナプロンプトプリセットテーブル。
    AI人格の性格・口調を定義するプロンプトを保持する。
    """

    __tablename__ = "persona_presets"

    # 主キー（UUID文字列）
    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ペルソナ定義テキスト（システムプロンプトに使用）
    persona_text: Mapped[str] = mapped_column(Text, nullable=False)

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AddonPreset(Base):
    """
    ペルソナ追加オプション（addon）プリセットテーブル。
    ペルソナプロンプトに追加する任意のテキストを保持する。
    """

    __tablename__ = "addon_presets"

    # 主キー（UUID文字列）
    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # アドオンテキスト（ペルソナプロンプトに追加される）
    addon_text: Mapped[str] = mapped_column(Text, nullable=False)

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmbeddingPreset(Base):
    """
    Embeddingプリセットテーブル。
    ベクトル埋め込みモデルの設定と記憶検索パラメータを保持する。
    """

    __tablename__ = "embedding_presets"

    # 主キー（UUID文字列、これが embedding_preset_id としても使用される）
    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    # name は表示名（embedding_preset_id ではない）
    name: Mapped[str] = mapped_column(String, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Embedding設定
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding_api_key: Mapped[Optional[str]] = mapped_column(String)
    embedding_base_url: Mapped[Optional[str]] = mapped_column(String)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)

    # 記憶検索パラメータ
    similar_episodes_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_inject_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1200)
    similar_limit_by_kind_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Reminder(Base):
    """
    リマインダー設定テーブル。
    指定日時にユーザーに通知する内容を保持する。
    """

    __tablename__ = "reminders"

    # 主キー（UUID文字列）
    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    # リマインダーの有効/無効
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 通知予定日時
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 通知内容
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # タイムスタンプ
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
