"""
リマインダーDB（reminders.db）のORMモデル

reminders.db は以下を扱う：
- グローバル設定（reminders_enabled / target_client_id）
- リマインダー定義（単発/毎日/毎週）
- 実行状態（次回発火時刻など）
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cocoro_ghost.reminders_db import RemindersBase


# UUIDの文字列長（ハイフン含む36文字）
_UUID_STR_LEN = 36
_CLIENT_ID_MAX_LEN = 128
_REPEAT_KIND_MAX_LEN = 16
_TIME_OF_DAY_MAX_LEN = 5  # "HH:MM"


def _uuid_str() -> str:
    """UUID文字列を生成するファクトリ関数。"""

    return str(uuid4())


class ReminderGlobalSettings(RemindersBase):
    """
    リマインダーのグローバル設定（単一行）。

    - reminders_enabled: リマインダー機能の有効/無効
    - target_client_id: 宛先 client_id（スピーカー端末）
    """

    __tablename__ = "reminder_global_settings"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_client_id: Mapped[Optional[str]] = mapped_column(String(_CLIENT_ID_MAX_LEN), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Reminder(RemindersBase):
    """
    リマインダー定義テーブル。

    repeat_kind:
    - once: scheduled_at_utc で単発
    - daily: time_of_day（HH:MM）
    - weekly: time_of_day（HH:MM） + weekdays_mask

    weekdays_mask:
    - 日曜を bit0 とする（Sun=1<<0, Mon=1<<1, ... Sat=1<<6）
    """

    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(_UUID_STR_LEN), primary_key=True, default=_uuid_str)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    repeat_kind: Mapped[str] = mapped_column(String(_REPEAT_KIND_MAX_LEN), nullable=False)

    # 単発（once）: 発火予定（UTC epoch seconds）
    scheduled_at_utc: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 繰り返し（daily/weekly）: 時刻（HH:MM）
    time_of_day: Mapped[Optional[str]] = mapped_column(String(_TIME_OF_DAY_MAX_LEN), nullable=True)

    # weekly: 対象曜日
    weekdays_mask: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 実行状態
    next_fire_at_utc: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_fired_at_utc: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
