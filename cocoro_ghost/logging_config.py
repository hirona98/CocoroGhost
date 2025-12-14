"""ロギング設定。"""

from __future__ import annotations

import logging


class _UvicornAccessPathFilter(logging.Filter):
    def __init__(self, suppressed_paths: set[str]) -> None:
        super().__init__()
        self._suppressed_paths = suppressed_paths

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(path in msg for path in self._suppressed_paths)


def suppress_uvicorn_access_log_paths(*paths: str) -> None:
    """uvicorn.access のアクセスログから特定パスの行だけ除外。"""
    if not paths:
        return
    logger = logging.getLogger("uvicorn.access")
    logger.addFilter(_UvicornAccessPathFilter(set(paths)))


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    # 外部ライブラリの冗長なログを抑制
    for name, lib_level in [
        ("LiteLLM", logging.INFO),
        ("litellm", logging.INFO),
        ("httpcore", logging.WARNING),
        ("httpx", logging.WARNING),
    ]:
        logging.getLogger(name).setLevel(lib_level)
