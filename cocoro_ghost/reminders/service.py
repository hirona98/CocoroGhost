"""
リマインダー（Reminders）実行サービス

役割:
- reminders.db を定期的に確認し、due なリマインダーを1件ずつ発火する。
- 発火時の文面生成と配信は MemoryManager に委譲する（AI人格のセリフ生成が必須）。

設計方針:
- cron無し運用を前提に、サーバ側の定期タスクから tick() を呼び出す。
- reminders_enabled が OFF のときは「完全に発火しない」。
- クライアント接続有無に依存せず、due 到達で発火する。
"""

from __future__ import annotations

import logging
import threading

from cocoro_ghost.core.clock import get_clock_service
from cocoro_ghost.autonomy.orchestrator import get_autonomy_orchestrator
from cocoro_ghost.app_bootstrap.dependencies import get_memory_manager
from cocoro_ghost.reminders.logic import (
    DEFAULT_REMINDER_TIME_ZONE,
    NextFireInput,
    compute_next_fire_at_utc,
    utc_ts_to_hhmm,
    validate_time_zone,
)
from cocoro_ghost.reminders.models import Reminder
from cocoro_ghost.reminders.repo import ensure_initial_reminder_global_settings
from cocoro_ghost.reminders.db import reminders_session_scope


logger = logging.getLogger(__name__)


def _now_utc_ts() -> int:
    """現在時刻（UTC epoch seconds）を返す。"""

    # --- リマインダー判定はdomain時刻を使う（時間前進検証を可能にする） ---
    return int(get_clock_service().now_domain_utc_ts())


class ReminderService:
    """
    リマインダーの状態管理と発火を行うサービス。

    - tick() は複数回呼ばれても安全（重複実行は抑制）。
    - ON/OFF 遷移をメモリ上で検出し、OFF→ON 直後のみ「過去分の捨て」を実行する。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._initialized = False
        self._enabled_prev = False

    def tick(self) -> None:
        """現在の設定/状態に基づき、due なリマインダーを必要な分だけ発火する。"""

        # --- 多重実行を抑止 ---
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            now_utc_ts = _now_utc_ts()

            # --- DB読み取り/更新 ---
            with reminders_session_scope() as db:
                settings = ensure_initial_reminder_global_settings(db)
                enabled = bool(settings.reminders_enabled)

                # --- 初回tick（起動直後） ---
                # NOTE:
                # - 起動時に enabled=True の場合は、過去分も「遅れて即発火」させたい。
                # - そのため、起動直後は OFF→ON 遷移扱いにしない（捨て処理はしない）。
                if not self._initialized:
                    self._initialized = True
                    self._enabled_prev = bool(enabled)

                # --- OFF時は完全に停止 ---
                if not enabled:
                    self._enabled_prev = False
                    return

                # --- OFF→ON 遷移: 過去分の捨て ---
                if not self._enabled_prev:
                    self._enabled_prev = True
                    self._drop_past_due_on_enable(db, now_utc_ts=now_utc_ts)

                # --- due を抽出（複数溜まることを許容） ---
                due = (
                    db.query(Reminder)
                    .filter(Reminder.enabled.is_(True))
                    .filter(Reminder.next_fire_at_utc.is_not(None))
                    .filter(Reminder.next_fire_at_utc <= int(now_utc_ts))
                    .order_by(Reminder.next_fire_at_utc.asc(), Reminder.created_at.asc())
                    .all()
                )
                if not due:
                    return

                # --- 順に発火（まとめない） ---
                mm = get_memory_manager()
                for r in due:
                    self._fire_one(
                        db,
                        mm=mm,
                        reminder=r,
                        now_utc_ts=now_utc_ts,
                    )
        finally:
            with self._lock:
                self._running = False

    def _drop_past_due_on_enable(self, db, *, now_utc_ts: int) -> None:
        """
        reminders_enabled を OFF→ON にした直後の「過去分捨て」を行う。

        仕様:
        - once: 過去分は削除
        - daily/weekly: 次回を未来に再計算
        """

        # --- 過去/現在の due を対象にする ---
        rows = (
            db.query(Reminder)
            .filter(Reminder.enabled.is_(True))
            .filter(Reminder.next_fire_at_utc.is_not(None))
            .filter(Reminder.next_fire_at_utc <= int(now_utc_ts))
            .order_by(Reminder.next_fire_at_utc.asc(), Reminder.created_at.asc())
            .all()
        )
        if not rows:
            return

        for r in rows:
            kind = str(r.repeat_kind or "").strip().lower()
            if kind == "once":
                db.delete(r)
                continue

            # --- 繰り返しは未来へ進める ---
            next_fire = self._compute_next_fire_at_utc(r, now_utc_ts=now_utc_ts)
            if next_fire is None:
                r.enabled = False
                r.next_fire_at_utc = None
                continue
            r.next_fire_at_utc = int(next_fire)

    def _fire_one(self, db, *, mm, reminder: Reminder, now_utc_ts: int) -> None:
        """due なリマインダーを1件発火し、DBの次回状態へ進める。"""

        # --- HH:MM（読み上げ/表示用） ---
        try:
            tz = validate_time_zone(str(DEFAULT_REMINDER_TIME_ZONE))
        except Exception:  # noqa: BLE001
            tz = validate_time_zone("UTC")

        next_fire_at_utc = int(reminder.next_fire_at_utc or 0)
        hhmm = utc_ts_to_hhmm(utc_ts=next_fire_at_utc, tz=tz)

        # --- 文面生成 + イベント配信 ---
        # NOTE:
        # - クライアント側の読み上げ制約があるため、メッセージは短い想定。
        # - memory_enabled=false でも配信は行う（保存はしない）。
        mm.run_reminder_once(
            hhmm=str(hhmm),
            content=str(reminder.content),
        )

        # --- 自発行動トリガ（time） ---
        # NOTE:
        # - reminder は Scheduler Policy のため、autonomy には event ではなく time trigger を投入する。
        # - trigger_key は「同じ reminder の同じ発火スロット」で一意にする。
        get_autonomy_orchestrator().enqueue_time_trigger(
            trigger_key=f"reminder:{str(reminder.id)}:{int(next_fire_at_utc)}",
            payload={
                "source": "reminder",
                "reminder_id": str(reminder.id),
                "scheduled_fire_at_utc": int(next_fire_at_utc),
                "hhmm": str(hhmm),
                "content": str(reminder.content),
                "repeat_kind": str(reminder.repeat_kind or ""),
            },
            scheduled_at=int(next_fire_at_utc),
        )

        # --- 次回へ進める（1回だけ発火） ---
        kind = str(reminder.repeat_kind or "").strip().lower()
        if kind == "once":
            db.delete(reminder)
            return

        reminder.last_fired_at_utc = int(now_utc_ts)
        next_fire = self._compute_next_fire_at_utc(reminder, now_utc_ts=now_utc_ts)
        if next_fire is None:
            reminder.enabled = False
            reminder.next_fire_at_utc = None
            return
        reminder.next_fire_at_utc = int(next_fire)

    def _compute_next_fire_at_utc(self, reminder: Reminder, *, now_utc_ts: int) -> int | None:
        """Reminder行から次回発火時刻（UTC epoch seconds）を計算する。"""

        try:
            next_fire = compute_next_fire_at_utc(
                NextFireInput(
                    now_utc_ts=int(now_utc_ts),
                    repeat_kind=str(reminder.repeat_kind),
                    time_zone=str(DEFAULT_REMINDER_TIME_ZONE),
                    scheduled_at_utc=(int(reminder.scheduled_at_utc) if reminder.scheduled_at_utc is not None else None),
                    time_of_day=(str(reminder.time_of_day) if reminder.time_of_day else None),
                    weekdays_mask=(int(reminder.weekdays_mask) if reminder.weekdays_mask is not None else None),
                )
            )
            return int(next_fire) if next_fire is not None else None
        except Exception:  # noqa: BLE001
            return None


_reminder_service = ReminderService()


def get_reminder_service() -> ReminderService:
    """ReminderService のシングルトンを返す。"""

    return _reminder_service
