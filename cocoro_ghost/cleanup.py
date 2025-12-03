"""生画像クリーンアップタスク。"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from cocoro_ghost import models
from cocoro_ghost.config import get_config_store
from cocoro_ghost.db import memory_session_scope


logger = logging.getLogger(__name__)


def cleanup_old_images(hours: int = 72) -> None:
    cutoff_time = datetime.utcnow() - timedelta(hours=hours)
    config_store = get_config_store()
    with memory_session_scope(config_store.memory_id, config_store.embedding_dimension) as db:
        episodes = (
            db.query(models.Episode)
            .filter(models.Episode.occurred_at < cutoff_time)
            .filter(
                (models.Episode.raw_desktop_path.isnot(None))
                | (models.Episode.raw_camera_path.isnot(None))
            )
            .all()
        )
        for ep in episodes:
            for path_attr in ["raw_desktop_path", "raw_camera_path"]:
                path = getattr(ep, path_attr)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("画像削除に失敗", exc_info=exc, extra={"path": path})
                        continue
                setattr(ep, path_attr, None)
        db.commit()
