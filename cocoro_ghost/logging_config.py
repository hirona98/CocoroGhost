"""
ロギング設定

アプリケーションのログ出力を設定する。
標準loggingの初期化、外部ライブラリのログ抑制、
uvicornアクセスログからの特定パス除外などを行う。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import pathlib


class _UvicornAccessPathFilter(logging.Filter):
    """
    uvicornアクセスログから特定パスを除外するフィルタ。

    ヘルスチェック等の頻繁なリクエストをログから除外する。
    """

    def __init__(self, suppressed_paths: set[str]) -> None:
        super().__init__()
        self._suppressed_paths = suppressed_paths

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """
        ログレコードのフィルタリングを行う。

        除外パスに一致しないログのみ通過させる。
        """
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(path in msg for path in self._suppressed_paths)


def suppress_uvicorn_access_log_paths(*paths: str) -> None:
    """
    uvicornアクセスログから特定パスを除外する。

    指定されたパスを含むアクセスログを出力しないようにする。
    """
    if not paths:
        return
    logger = logging.getLogger("uvicorn.access")
    logger.addFilter(_UvicornAccessPathFilter(set(paths)))


def setup_logging(
    level: str = "INFO",
    *,
    log_file_enabled: bool = False,
    log_file_path: str = "logs/cocoro_ghost.log",
) -> None:
    """
    ロギングを初期化する。

    標準loggingのフォーマット設定と、外部ライブラリのログレベル調整を行う。
    """
    root_level = getattr(logging, level.upper(), logging.INFO)
    console_handler = logging.StreamHandler()
    handlers: list[logging.Handler] = [console_handler]
    file_handler: logging.Handler | None = None
    if log_file_enabled:
        log_path = pathlib.Path(log_file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # ファイルログは最大1MBでローテーションしてサイズ超過を防ぐ。
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=1,
            encoding="utf-8",
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers or None,
    )
    _setup_llm_io_loggers(root_level, console_handler, file_handler)
    # 外部ライブラリの冗長なログを抑制
    for name, lib_level in [
        # Windows環境で asyncio が出す "Using proactor: IocpProactor" などのDEBUGを抑制する
        ("asyncio", logging.INFO),
        ("LiteLLM", logging.INFO),
        ("litellm", logging.INFO),
        ("openai", logging.INFO),
        # NOTE: アプリ側（cocoro_ghost.*）のログレベルは root(level) に従わせる。
        ("httpcore", logging.WARNING),
        ("httpx", logging.WARNING),
    ]:
        logging.getLogger(name).setLevel(lib_level)


def _setup_llm_io_loggers(
    root_level: int,
    console_handler: logging.Handler,
    file_handler: logging.Handler | None,
) -> None:
    """LLM送受信ログ用のロガーを出力先ごとに初期化する。"""
    console_logger = logging.getLogger("cocoro_ghost.llm_io.console")
    console_logger.handlers.clear()
    console_logger.addHandler(console_handler)
    console_logger.setLevel(root_level)
    console_logger.propagate = False

    file_logger = logging.getLogger("cocoro_ghost.llm_io.file")
    file_logger.handlers.clear()
    if file_handler is None:
        file_logger.addHandler(logging.NullHandler())
    else:
        file_logger.addHandler(file_handler)
    file_logger.setLevel(root_level)
    file_logger.propagate = False
