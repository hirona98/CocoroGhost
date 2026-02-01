"""
時刻ユーティリティ

このモジュールは、DBに保存しているUNIX秒（UTC）を
LLMへ渡しやすい ISO 8601 のローカル時刻へ変換する用途で使う。

注意:
- DB自体の保存形式（UNIX秒）は変更しない（検索・ソートが簡単なため）。
- ここでの「ローカル」は実行環境のローカルタイムゾーンを指す。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def format_iso8601_local(ts_utc: Optional[int]) -> Optional[str]:
    """UTCのUNIX秒を、ISO 8601形式のローカル時刻へ変換して返す（タイムゾーン表記なし）。

    例:
    - 1700000000 -> "2023-11-14T07:13:20"（環境がJSTの場合）

    Args:
        ts_utc: UTCのUNIX秒（int）。None/0以下はNoneを返す。

    Returns:
        ISO 8601形式のローカル時刻（秒精度）。無効値ならNone。
    """

    # --- 無効値は None ---
    if ts_utc is None:
        return None
    try:
        ts_i = int(ts_utc)
    except Exception:  # noqa: BLE001
        return None
    if ts_i <= 0:
        return None

    # --- UTC -> ローカル ---
    dt_utc = datetime.fromtimestamp(ts_i, tz=timezone.utc)
    dt_local = dt_utc.astimezone()
    # NOTE:
    # - 表示は「ローカル時刻」だが、LLMへ渡すときはタイムゾーン表記を付けない（ユーザー要望）。
    # - ただし tzinfo は内部的に保持したままなので、ここで naive に落として文字列化する。
    dt_local_naive = dt_local.replace(tzinfo=None)
    return dt_local_naive.isoformat(timespec="seconds")


def format_iso8601_local_with_tz(ts_utc: Optional[int]) -> Optional[str]:
    """UTCのUNIX秒を、ISO 8601形式のローカル時刻へ変換して返す（タイムゾーン表記あり）。

    例:
    - 1700000000 -> "2023-11-14T07:13:20+09:00"（環境がJSTの場合）

    Args:
        ts_utc: UTCのUNIX秒（int）。None/0以下はNoneを返す。

    Returns:
        ISO 8601形式のローカル時刻（秒精度）。無効値ならNone。
    """

    # --- 無効値は None ---
    if ts_utc is None:
        return None
    try:
        ts_i = int(ts_utc)
    except Exception:  # noqa: BLE001
        return None
    if ts_i <= 0:
        return None

    # --- UTC -> ローカル（tzinfo保持） ---
    dt_utc = datetime.fromtimestamp(ts_i, tz=timezone.utc)
    dt_local = dt_utc.astimezone()
    return dt_local.isoformat(timespec="seconds")


def parse_iso8601_to_utc_ts(text_in: Optional[str]) -> Optional[int]:
    """
    ISO 8601 形式の日時文字列を UTC のUNIX秒へ変換して返す。

    目的:
        - LLMが読みやすい ISO 8601（タイムゾーン無し/あり両方）を受け取りつつ、
          DBはUNIX秒（UTC）で保持する設計を維持する。

    注意:
        - タイムゾーン情報が無い場合は「ローカル時刻」とみなし、UTCへ変換する。
        - 不正な文字列は None を返す（DBを壊さないため）。
    """
    s = str(text_in or "").strip()
    if not s:
        return None

    try:
        dt = datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None

    # --- タイムゾーン無しはローカルとして扱う ---
    if dt.tzinfo is None:
        try:
            local_tz = datetime.now().astimezone().tzinfo
            dt = dt.replace(tzinfo=local_tz)
        except Exception:  # noqa: BLE001
            return None

    try:
        dt_utc = dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None

    try:
        return int(dt_utc.timestamp())
    except Exception:  # noqa: BLE001
        return None
