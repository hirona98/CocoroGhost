"""
リマインダーDB（reminders.db）接続とセッション管理

リマインダーは「設定」ではなく「実行状態（次回発火時刻など）を持つスケジューラ対象」なので、
settings.db とは分離して reminders.db として管理する。
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


logger = logging.getLogger(__name__)

# reminders.db 用 Base
RemindersBase = declarative_base()

# グローバルセッション（reminders.db 用）
RemindersSessionLocal: sessionmaker | None = None


@dataclasses.dataclass(frozen=True)
class RemindersDbPaths:
    """reminders.db のパス群（将来の分離に備えた薄いラッパ）。"""

    data_dir: Path
    reminders_db_path: Path


def get_data_dir() -> Path:
    """
    data ディレクトリを返す。

    NOTE:
    - settings.db / memory_*.db と同じ data/ 配下に置く。
    """

    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


def get_reminders_db_paths() -> RemindersDbPaths:
    """reminders.db のパス情報を返す。"""

    data_dir = get_data_dir()
    return RemindersDbPaths(
        data_dir=data_dir,
        reminders_db_path=(data_dir / "reminders.db"),
    )


def get_reminders_db_url() -> str:
    """reminders.db のSQLAlchemy URLを返す。"""

    p = get_reminders_db_paths().reminders_db_path
    return f"sqlite:///{p}"


def init_reminders_db() -> None:
    """
    reminders.db を初期化する（起動時）。

    - セッションファクトリを作成する
    - テーブルを作成する
    """

    global RemindersSessionLocal

    db_url = get_reminders_db_url()
    connect_args = {"check_same_thread": False, "timeout": 10.0}
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    RemindersSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    # reminders.db のテーブル群を作成（モデル import が必要）
    import cocoro_ghost.reminders_models  # noqa: F401

    RemindersBase.metadata.create_all(bind=engine)
    logger.info("reminders DB initialized: %s", db_url)


def get_reminders_db() -> Iterator[Session]:
    """
    reminders.db のセッションを取得する（FastAPI依存性注入用）。

    使用後は自動でクローズされる。
    """

    if RemindersSessionLocal is None:
        raise RuntimeError("Reminders database not initialized. Call init_reminders_db() first.")
    session = RemindersSessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextlib.contextmanager
def reminders_session_scope() -> Iterator[Session]:
    """
    reminders.db のセッションスコープ（with文用）。

    正常終了時はコミット、例外時はロールバックする。
    """

    if RemindersSessionLocal is None:
        raise RuntimeError("Reminders database not initialized. Call init_reminders_db() first.")
    session = RemindersSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

