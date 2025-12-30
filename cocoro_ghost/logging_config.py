"""
ロギング設定

アプリケーションのログ出力を設定する。
標準loggingの初期化、外部ライブラリのログ抑制、
uvicornアクセスログからの特定パス除外などを行う。
"""

from __future__ import annotations

import logging


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


def setup_logging(level: str = "INFO") -> None:
    """
    ロギングを初期化する。

    標準loggingのフォーマット設定と、外部ライブラリのログレベル調整を行う。
    """
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
        # NOTE: アプリ側（cocoro_ghost.*）のログレベルは root(level) に従わせる。
        ("httpcore", logging.WARNING),
        ("httpx", logging.WARNING),
    ]:
        logging.getLogger(name).setLevel(lib_level)
