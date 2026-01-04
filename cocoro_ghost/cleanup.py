"""生画像クリーンアップタスク。

Unitベース移行後は、生画像パスをDBに保持しないため、任意ディレクトリ配下の古いファイルを掃除する。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cocoro_ghost.paths import get_data_dir


logger = logging.getLogger(__name__)


def cleanup_old_images(hours: int = 72, raw_dir_name: str = "raw_images") -> None:
    """データディレクトリ配下の生画像を、指定時間より古いものから削除する。"""
    raw_dir = Path(get_data_dir()) / raw_dir_name
    if not raw_dir.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for path in raw_dir.glob("**/*"):
        if not path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像削除に失敗", exc_info=exc, extra={"path": str(path)})
