"""DB 接続とセッション管理。"""

from __future__ import annotations

import contextlib
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from cocoro_ghost.config import get_config_store


Base = declarative_base()
SessionLocal: sessionmaker | None = None


def _enable_sqlite_vec(engine, dimension: int) -> None:
    with engine.connect() as conn:
        conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS episode_embeddings USING vec0(embedding float[{dimension}]));"))
        conn.commit()


def init_db(db_url: str | None = None) -> None:
    global SessionLocal
    url = db_url or get_config_store().config.db_url
    engine = create_engine(url, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    if url.startswith("sqlite"):
        dim = get_config_store().config.embedding_dimension
        _enable_sqlite_vec(engine, dim)


def get_db() -> Iterator:
    if SessionLocal is None:
        init_db()
    assert SessionLocal is not None
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def session_scope() -> Iterator:
    if SessionLocal is None:
        init_db()
    assert SessionLocal is not None
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert_episode_embedding(session, episode_id: int, embedding: list[float]) -> None:
    session.execute(
        text(
            """
            INSERT INTO episode_embeddings(rowid, embedding)
            VALUES (:episode_id, :embedding)
            ON CONFLICT(rowid) DO UPDATE SET embedding = excluded.embedding
            """
        ),
        {"episode_id": episode_id, "embedding": embedding},
    )


def search_similar_episodes(session, query_embedding: list[float], limit: int = 5):
    rows = session.execute(
        text(
            """
            SELECT rowid as episode_id, distance
            FROM episode_embeddings
            WHERE embedding MATCH :query
            ORDER BY distance ASC
            LIMIT :limit
            """
        ),
        {"query": query_embedding, "limit": limit},
    ).fetchall()
    return rows
