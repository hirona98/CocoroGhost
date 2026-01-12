"""
リマインダーDBのリポジトリ（最小のCRUD補助）

目的:
- reminders.db は settings.db と独立しているため、初期化（単一行の作成など）をここに集約する。
- API/サービスから同じロジックを呼べるようにする。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from cocoro_ghost.reminders_models import ReminderGlobalSettings


def ensure_initial_reminder_global_settings(db: Session) -> ReminderGlobalSettings:
    """
    リマインダーのグローバル設定（単一行）を必ず存在させて返す。

    方針:
    - 未作成ならデフォルト値で作成する（reminders_enabled=False）。
    """

    # --- 既存行を取得（最初の1行を採用） ---
    row = db.query(ReminderGlobalSettings).order_by(ReminderGlobalSettings.created_at.asc()).first()
    if row is not None:
        return row

    # --- 初期行を作成 ---
    row = ReminderGlobalSettings(
        reminders_enabled=False,
    )
    db.add(row)
    db.flush()
    return row
