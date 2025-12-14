"""ロギング設定。"""

from __future__ import annotations

import logging


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
