"""
リマインダーAPI（/api/reminders/*）

目的:
- /api/settings とは独立に、リマインダー（単発/毎日/毎週）をCRUDする。
- 発火処理はサーバ側の ReminderService が担当する（このAPIは設定/状態の更新のみ）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from cocoro_ghost import schemas
from cocoro_ghost.deps import get_reminders_db_dep
from cocoro_ghost.reminders_logic import (
    DEFAULT_REMINDER_TIME_ZONE,
    NextFireInput,
    compute_next_fire_at_utc,
    mask_to_weekdays,
    parse_hhmm,
    parse_scheduled_at_to_utc_ts,
    utc_ts_to_iso_in_tz,
    validate_repeat_kind,
    validate_time_zone,
    weekdays_to_mask,
)
from cocoro_ghost.reminders_models import Reminder
from cocoro_ghost.reminders_repo import ensure_initial_reminder_global_settings


router = APIRouter(prefix="/reminders", tags=["reminders"])


def _normalize_client_id(value: str | None) -> str | None:
    """client_id を正規化し、空文字なら None を返す。"""

    s = str(value or "").strip()
    return s or None


def _require_non_empty(value: str | None, *, field: str) -> str:
    """必須の文字列フィールドを検証する。"""

    s = str(value or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    return s


def _reminder_to_item(r: Reminder) -> schemas.ReminderItem:
    """ORMのReminderをAPIレスポンスのReminderItemへ変換する。"""

    # --- timezone の検証（保存時に弾く想定だが、念のためここでも保守的に扱う） ---
    try:
        tz = validate_time_zone(str(DEFAULT_REMINDER_TIME_ZONE))
    except Exception:  # noqa: BLE001
        tz = validate_time_zone("UTC")

    # --- once 用 scheduled_at（offset付きISOに戻す） ---
    scheduled_at = None
    if str(r.repeat_kind) == "once" and r.scheduled_at_utc is not None:
        scheduled_at = utc_ts_to_iso_in_tz(utc_ts=int(r.scheduled_at_utc), tz=tz)

    # --- weekly の weekdays ---
    weekdays: list[str] = []
    if str(r.repeat_kind) == "weekly" and r.weekdays_mask is not None:
        weekdays = mask_to_weekdays(int(r.weekdays_mask))

    return schemas.ReminderItem(
        id=str(r.id),
        enabled=bool(r.enabled),
        repeat_kind=str(r.repeat_kind),
        content=str(r.content),
        scheduled_at=scheduled_at,
        time_of_day=(str(r.time_of_day) if r.time_of_day else None),
        weekdays=list(weekdays),
        next_fire_at_utc=(int(r.next_fire_at_utc) if r.next_fire_at_utc is not None else None),
    )


def _compute_next_fire_or_400(
    *,
    now_utc_ts: int,
    repeat_kind: str,
    scheduled_at_utc: int | None,
    time_of_day: str | None,
    weekdays_mask: int | None,
) -> int | None:
    """次回発火を計算し、必須条件が満たされない場合は400にする。"""

    try:
        next_fire = compute_next_fire_at_utc(
            NextFireInput(
                now_utc_ts=int(now_utc_ts),
                repeat_kind=str(repeat_kind),
                time_zone=str(DEFAULT_REMINDER_TIME_ZONE),
                scheduled_at_utc=(int(scheduled_at_utc) if scheduled_at_utc is not None else None),
                time_of_day=(str(time_of_day) if time_of_day else None),
                weekdays_mask=(int(weekdays_mask) if weekdays_mask is not None else None),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return int(next_fire) if next_fire is not None else None


def _apply_create_request_to_model(
    *,
    now_utc_ts: int,
    request: schemas.ReminderCreateRequest,
) -> Reminder:
    """作成リクエストを検証し、ReminderのORMインスタンスを構築する。"""

    # --- 入力の正規化 ---
    repeat_kind = validate_repeat_kind(request.repeat_kind)
    content = _require_non_empty(request.content, field="content")

    # --- repeat_kind ごとの必須フィールド ---
    scheduled_at_utc: int | None = None
    time_of_day: str | None = None
    weekdays_mask: int | None = None

    if repeat_kind == "once":
        scheduled_at_utc = parse_scheduled_at_to_utc_ts(_require_non_empty(request.scheduled_at, field="scheduled_at"))
    elif repeat_kind == "daily":
        _ = parse_hhmm(_require_non_empty(request.time_of_day, field="time_of_day"))
        time_of_day = str(request.time_of_day).strip()
    else:
        _ = parse_hhmm(_require_non_empty(request.time_of_day, field="time_of_day"))
        time_of_day = str(request.time_of_day).strip()
        weekdays_mask = weekdays_to_mask(list(request.weekdays or []))
        if int(weekdays_mask) == 0:
            raise HTTPException(status_code=400, detail="weekdays is required for weekly")

    # --- 次回発火の計算 ---
    next_fire_at_utc = _compute_next_fire_or_400(
        now_utc_ts=now_utc_ts,
        repeat_kind=repeat_kind,
        scheduled_at_utc=scheduled_at_utc,
        time_of_day=time_of_day,
        weekdays_mask=weekdays_mask,
    )

    # --- ORM構築 ---
    return Reminder(
        enabled=bool(request.enabled),
        repeat_kind=repeat_kind,
        scheduled_at_utc=scheduled_at_utc,
        time_of_day=time_of_day,
        weekdays_mask=weekdays_mask,
        content=content,
        next_fire_at_utc=next_fire_at_utc,
        last_fired_at_utc=None,
    )


def _apply_update_request_inplace(
    *,
    now_utc_ts: int,
    reminder: Reminder,
    request: schemas.ReminderUpdateRequest,
) -> None:
    """
    更新リクエストをReminderへ適用し、next_fire_at_utc を再計算する。

    重要:
    - 「編集由来で next_fire_at が過去」になった場合は、過去分を捨てる（即発火しない）。
    """

    # --- 変更適用（単純な項目） ---
    if request.enabled is not None:
        reminder.enabled = bool(request.enabled)
    if request.content is not None:
        reminder.content = _require_non_empty(request.content, field="content")
    if request.repeat_kind is not None:
        reminder.repeat_kind = validate_repeat_kind(request.repeat_kind)

    # --- repeat_kind ごとの項目 ---
    kind = validate_repeat_kind(reminder.repeat_kind)

    if kind == "once":
        # weekly/daily のフィールドは無効化
        reminder.time_of_day = None
        reminder.weekdays_mask = None

        if request.scheduled_at is not None:
            reminder.scheduled_at_utc = parse_scheduled_at_to_utc_ts(_require_non_empty(request.scheduled_at, field="scheduled_at"))
    elif kind == "daily":
        # once/weekly のフィールドは無効化
        reminder.scheduled_at_utc = None
        reminder.weekdays_mask = None

        if request.time_of_day is not None:
            _ = parse_hhmm(_require_non_empty(request.time_of_day, field="time_of_day"))
            reminder.time_of_day = str(request.time_of_day).strip()
    else:
        # once のフィールドは無効化
        reminder.scheduled_at_utc = None

        if request.time_of_day is not None:
            _ = parse_hhmm(_require_non_empty(request.time_of_day, field="time_of_day"))
            reminder.time_of_day = str(request.time_of_day).strip()
        if request.weekdays is not None:
            reminder.weekdays_mask = weekdays_to_mask(list(request.weekdays or []))
            if int(reminder.weekdays_mask or 0) == 0:
                raise HTTPException(status_code=400, detail="weekdays is required for weekly")

    # --- 次回発火の再計算 ---
    reminder.next_fire_at_utc = _compute_next_fire_or_400(
        now_utc_ts=now_utc_ts,
        repeat_kind=kind,
        scheduled_at_utc=(int(reminder.scheduled_at_utc) if reminder.scheduled_at_utc is not None else None),
        time_of_day=(str(reminder.time_of_day) if reminder.time_of_day else None),
        weekdays_mask=(int(reminder.weekdays_mask) if reminder.weekdays_mask is not None else None),
    )

    # --- 編集由来の「過去 next_fire」をスキップ ---
    # NOTE:
    # - daily/weekly は compute が「次の未来」を返すため、ここで過去になるのは通常あり得ない。
    # - once は scheduled_at を過去に編集した場合、即発火してほしくないため、無効化する。
    if kind == "once" and request.scheduled_at is not None:
        if reminder.next_fire_at_utc is not None and int(reminder.next_fire_at_utc) <= int(now_utc_ts):
            reminder.enabled = False
            reminder.next_fire_at_utc = None


@router.get("/settings", response_model=schemas.RemindersGlobalSettingsResponse)
def get_reminder_settings(db: Session = Depends(get_reminders_db_dep)) -> schemas.RemindersGlobalSettingsResponse:
    """リマインダーのグローバル設定を取得する。"""

    row = ensure_initial_reminder_global_settings(db)
    return schemas.RemindersGlobalSettingsResponse(
        reminders_enabled=bool(row.reminders_enabled),
        target_client_id=_normalize_client_id(row.target_client_id),
    )


@router.put("/settings", response_model=schemas.RemindersGlobalSettingsResponse)
def put_reminder_settings(
    request: schemas.RemindersGlobalSettingsUpdateRequest,
    db: Session = Depends(get_reminders_db_dep),
) -> schemas.RemindersGlobalSettingsResponse:
    """リマインダーのグローバル設定を更新する。"""

    row = ensure_initial_reminder_global_settings(db)

    # --- 更新 ---
    row.reminders_enabled = bool(request.reminders_enabled)
    row.target_client_id = _normalize_client_id(request.target_client_id)

    db.commit()

    return schemas.RemindersGlobalSettingsResponse(
        reminders_enabled=bool(row.reminders_enabled),
        target_client_id=_normalize_client_id(row.target_client_id),
    )


@router.get("", response_model=schemas.RemindersListResponse)
def list_reminders(db: Session = Depends(get_reminders_db_dep)) -> schemas.RemindersListResponse:
    """リマインダー一覧を返す。"""

    # --- next_fire_at が None のものは末尾へ寄せる ---
    items = (
        db.query(Reminder)
        .order_by(
            (Reminder.next_fire_at_utc.is_(None)).asc(),
            Reminder.next_fire_at_utc.asc(),
            Reminder.created_at.asc(),
        )
        .all()
    )
    return schemas.RemindersListResponse(items=[_reminder_to_item(r) for r in items])


@router.post("", response_model=schemas.ReminderItem)
def create_reminder(
    request: schemas.ReminderCreateRequest,
    db: Session = Depends(get_reminders_db_dep),
) -> schemas.ReminderItem:
    """リマインダーを作成する。"""

    now_utc_ts = int(time.time())

    # --- 作成 ---
    reminder = _apply_create_request_to_model(now_utc_ts=now_utc_ts, request=request)
    db.add(reminder)
    db.commit()
    db.refresh(reminder)

    return _reminder_to_item(reminder)


@router.patch("/{reminder_id}", response_model=schemas.ReminderItem)
def update_reminder(
    reminder_id: str,
    request: schemas.ReminderUpdateRequest,
    db: Session = Depends(get_reminders_db_dep),
) -> schemas.ReminderItem:
    """リマインダーを更新する（部分更新）。"""

    now_utc_ts = int(time.time())

    # --- 対象取得 ---
    reminder = db.query(Reminder).filter_by(id=str(reminder_id)).first()
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")

    # --- 適用 ---
    _apply_update_request_inplace(now_utc_ts=now_utc_ts, reminder=reminder, request=request)

    db.commit()
    db.refresh(reminder)

    return _reminder_to_item(reminder)


@router.delete("/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reminder(reminder_id: str, db: Session = Depends(get_reminders_db_dep)) -> Response:
    """リマインダーを削除する。"""

    reminder = db.query(Reminder).filter_by(id=str(reminder_id)).first()
    if reminder is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    db.delete(reminder)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
