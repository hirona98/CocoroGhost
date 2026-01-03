"""
リマインダーの正規化・次回計算ロジック

このモジュールは「DB/HTTP/LLM」に依存しない純粋ロジックとして扱う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


WEEKDAYS_SUN_FIRST: list[str] = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_WEEKDAY_TO_BIT: dict[str, int] = {d: (1 << i) for i, d in enumerate(WEEKDAYS_SUN_FIRST)}


def validate_time_zone(tz_name: str) -> ZoneInfo:
    """
    IANA time zone を検証して ZoneInfo を返す。

    例: "Asia/Tokyo"
    """

    name = str(tz_name or "").strip()
    if not name:
        raise ValueError("time_zone is required")

    # --- ZoneInfo の生成で検証する ---
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid time_zone: {name}") from exc


def validate_repeat_kind(kind: str) -> str:
    """repeat_kind を正規化して返す（once/daily/weekly）。"""

    k = str(kind or "").strip().lower()
    if k not in {"once", "daily", "weekly"}:
        raise ValueError("repeat_kind must be one of: once, daily, weekly")
    return k


def parse_hhmm(value: str) -> tuple[int, int]:
    """HH:MM を (hour, minute) にパースする。"""

    s = str(value or "").strip()
    if not _HHMM_RE.match(s):
        raise ValueError("time_of_day must be in HH:MM format")
    hour = int(s[0:2])
    minute = int(s[3:5])
    if not (0 <= hour <= 23):
        raise ValueError("time_of_day hour must be 00..23")
    if not (0 <= minute <= 59):
        raise ValueError("time_of_day minute must be 00..59")
    return hour, minute


def normalize_weekdays(values: list[str]) -> list[str]:
    """
    weekdays を正規化する。

    - 小文字化
    - 重複排除
    - Sun-first の順でソート
    """

    seen: set[str] = set()
    normalized: list[str] = []
    for raw in list(values or []):
        s = str(raw or "").strip().lower()
        if not s:
            continue
        if s not in _WEEKDAY_TO_BIT:
            raise ValueError(f"invalid weekday: {s} (expected one of {WEEKDAYS_SUN_FIRST})")
        if s in seen:
            continue
        seen.add(s)
        normalized.append(s)

    # --- Sun-first に整列 ---
    normalized.sort(key=lambda d: WEEKDAYS_SUN_FIRST.index(d))
    return normalized


def weekdays_to_mask(weekdays: list[str]) -> int:
    """weekdays（sun..sat）を bitmask に変換する（Sun=bit0）。"""

    wd = normalize_weekdays(weekdays)
    mask = 0
    for d in wd:
        mask |= int(_WEEKDAY_TO_BIT[d])
    return int(mask)


def mask_to_weekdays(mask: int) -> list[str]:
    """bitmask（Sun=bit0）を weekdays の配列へ戻す（Sun-first）。"""

    m = int(mask or 0)
    out: list[str] = []
    for d in WEEKDAYS_SUN_FIRST:
        if m & int(_WEEKDAY_TO_BIT[d]):
            out.append(d)
    return out


def parse_scheduled_at_to_utc_ts(value: str) -> int:
    """
    scheduled_at（ISO8601, offset必須）を UTC epoch seconds に変換する。

    NOTE:
    - "Z" を "+00:00" として扱う。
    """

    s = str(value or "").strip()
    if not s:
        raise ValueError("scheduled_at is required")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("scheduled_at must be ISO 8601 datetime") from exc
    if dt.tzinfo is None:
        raise ValueError("scheduled_at must include timezone offset (e.g. +09:00)")
    return int(dt.astimezone(timezone.utc).timestamp())


def utc_ts_to_hhmm(*, utc_ts: int, tz: ZoneInfo) -> str:
    """UTC epoch seconds を指定TZの HH:MM へ変換する。"""

    dt = datetime.fromtimestamp(int(utc_ts), tz=tz)
    return dt.strftime("%H:%M")


def utc_ts_to_iso_in_tz(*, utc_ts: int, tz: ZoneInfo) -> str:
    """UTC epoch seconds を指定TZの ISO8601 へ変換する（offset付き）。"""

    dt = datetime.fromtimestamp(int(utc_ts), tz=tz)
    return dt.isoformat()


@dataclass(frozen=True)
class NextFireInput:
    """次回発火時刻の計算入力。"""

    now_utc_ts: int
    repeat_kind: str
    time_zone: str
    scheduled_at_utc: int | None
    time_of_day: str | None
    weekdays_mask: int | None


def compute_next_fire_at_utc(inp: NextFireInput) -> int | None:
    """
    次回発火時刻（UTC epoch seconds）を計算する。

    返り値:
    - None: 次回が無い（例: once だが scheduled_at が無い、または無効扱い）

    方針:
    - daily/weekly は「次の未来」を返す（= now と同時刻は次回扱い）。
    - missed（停止などで過去分が溜まった）を回数分消化しない。
      → 1回だけ発火し、その後は次の未来へ進める前提。
    """

    now_utc_ts = int(inp.now_utc_ts)
    kind = validate_repeat_kind(inp.repeat_kind)
    tz = validate_time_zone(inp.time_zone)

    # --- once: scheduled_at のみ ---
    if kind == "once":
        if inp.scheduled_at_utc is None:
            return None
        return int(inp.scheduled_at_utc)

    # --- daily/weekly: time_of_day 必須 ---
    if not inp.time_of_day:
        return None
    hour, minute = parse_hhmm(inp.time_of_day)

    now_local = datetime.fromtimestamp(now_utc_ts, tz=tz)
    base_date = now_local.date()

    # --- daily ---
    if kind == "daily":
        cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now_local:
            cand = cand + timedelta(days=1)
        return int(cand.timestamp())

    # --- weekly ---
    mask = int(inp.weekdays_mask or 0)
    if mask == 0:
        return None

    # 日付を前に進め、最初に合致する曜日を採用する
    for offset in range(0, 8):
        d = base_date + timedelta(days=offset)

        # Python weekday: Mon=0..Sun=6
        # Sun-first bit: Sun=0..Sat=6
        sun_first_idx = (d.weekday() + 1) % 7
        bit = 1 << int(sun_first_idx)
        if not (mask & bit):
            continue

        cand_local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
        if cand_local <= now_local:
            continue
        return int(cand_local.timestamp())

    # 1週間内で見つからないのは mask 不正のはずだが、保守的に None
    return None

