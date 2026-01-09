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
    """UTCのUNIX秒を、ISO 8601形式のローカル時刻へ変換して返す。

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

    # --- UTC -> ローカル ---
    dt_utc = datetime.fromtimestamp(ts_i, tz=timezone.utc)
    dt_local = dt_utc.astimezone()
    return dt_local.isoformat(timespec="seconds")

