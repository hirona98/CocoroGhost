"""ロギング設定。"""

from __future__ import annotations

import logging


def _install_debug2_level() -> None:
    """DEBUGよりも詳細なログレベル(DEBUG2)を追加する。"""
    if hasattr(logging, "DEBUG2"):
        return
    # DEBUGより詳細なログを分離するための独自レベル。
    logging.DEBUG2 = 5  # type: ignore[attr-defined]
    logging.addLevelName(logging.DEBUG2, "DEBUG2")

    def debug2(self: logging.Logger, msg: object, *args: object, **kwargs: object) -> None:
        if self.isEnabledFor(logging.DEBUG2):
            self._log(logging.DEBUG2, msg, args, **kwargs)

    def root_debug2(msg: object, *args: object, **kwargs: object) -> None:
        logging.getLogger().debug2(msg, *args, **kwargs)

    logging.Logger.debug2 = debug2  # type: ignore[assignment]
    logging.debug2 = root_debug2  # type: ignore[assignment]


class _UvicornAccessPathFilter(logging.Filter):
    """uvicorn.accessログから特定パスを除外するためのFilter。"""

    def __init__(self, suppressed_paths: set[str]) -> None:
        super().__init__()
        self._suppressed_paths = suppressed_paths

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """Trueなら通す。suppressed_pathsに一致するアクセスログだけ落とす。"""
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
    """標準loggingの初期化と、外部ライブラリのログレベル調整を行う。"""
    _install_debug2_level()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    # 外部ライブラリの冗長なログを抑制
    for name, lib_level in [
        # Windows環境で asyncio が出す "Using proactor: IocpProactor" などのDEBUGを抑制する
        ("asyncio", logging.INFO),
        ("LiteLLM", logging.INFO),
        ("litellm", logging.INFO),
        ("openai", logging.INFO),
        ("cocoro_ghost.llm_client", logging.DEBUG),
        ("httpcore", logging.WARNING),
        ("httpx", logging.WARNING),
    ]:
        logging.getLogger(name).setLevel(lib_level)
