"""DB 接続とセッション管理。"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from cocoro_ghost.config import get_config_store


Base = declarative_base()
SessionLocal: sessionmaker | None = None

logger = logging.getLogger(__name__)


def _enable_sqlite_vec(engine, dimension: int) -> None:
    import sqlite_vec
    vec_path = Path(sqlite_vec.__file__).parent / "vec0"
    with engine.connect() as conn:
        try:
            conn.exec_driver_sql(f"SELECT load_extension('{vec_path}');")
        except Exception as exc:  # noqa: BLE001
            logger.error("sqlite-vec拡張のロードに失敗しました", exc_info=exc)
            raise
        conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS episode_embeddings USING vec0(embedding float[{dimension}])"))
        conn.commit()


def init_db(db_url: str, embedding_dimension: int) -> None:
    global SessionLocal
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, future=True, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_load_extension(dbapi_conn, connection_record):
            dbapi_conn.enable_load_extension(True)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    if db_url.startswith("sqlite"):
        _enable_sqlite_vec(engine, embedding_dimension)


def get_db() -> Iterator:
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def session_scope() -> Iterator:
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
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
    import json
    embedding_json = json.dumps(embedding)
    session.execute(text("DELETE FROM episode_embeddings WHERE rowid = :episode_id"), {"episode_id": episode_id})
    session.execute(
        text("INSERT INTO episode_embeddings(rowid, embedding) VALUES (:episode_id, :embedding)"),
        {"episode_id": episode_id, "embedding": embedding_json},
    )


def search_similar_episodes(session, query_embedding: list[float], limit: int = 5):
    import json
    query_json = json.dumps(query_embedding)
    rows = session.execute(
        text(
            """
            SELECT rowid as episode_id, distance
            FROM episode_embeddings
            WHERE embedding MATCH :query
              AND k = :limit
            ORDER BY distance ASC
            """
        ),
        {"query": query_json, "limit": limit},
    ).fetchall()
    return rows


def migrate_toml_to_db_if_needed(session, toml_config) -> None:
    """TOMLからDBへの初回マイグレーション（プリセットが0件の場合のみ）"""
    import json
    from cocoro_ghost import models

    count = session.query(models.SettingPreset).count()
    if count == 0:
        if not toml_config.llm_api_key or not toml_config.llm_model:
            logger.warning("TOMLにLLM設定が無いため、マイグレーションをスキップします。手動でプリセットを作成してください。")
            return

        logger.info("プリセットが存在しないため、TOML設定から'default'プリセットを作成します")
        default_preset = models.SettingPreset(
            name="default",
            is_active=True,
            llm_api_key=toml_config.llm_api_key,
            llm_model=toml_config.llm_model,
            reflection_model=toml_config.reflection_model,
            embedding_model=toml_config.embedding_model,
            embedding_dimension=toml_config.embedding_dimension,
            image_model=toml_config.image_model,
            image_timeout_seconds=toml_config.image_timeout_seconds,
            character_prompt=toml_config.character_prompt,
            intervention_level=toml_config.intervention_level,
            exclude_keywords=json.dumps(toml_config.exclude_keywords),
            similar_episodes_limit=toml_config.similar_episodes_limit,
            max_chat_queue=toml_config.max_chat_queue,
        )
        session.add(default_preset)
        logger.info("'default'プリセットを作成しました")


def load_active_preset_from_db(session):
    """アクティブなプリセットをDBから取得"""
    from cocoro_ghost import models

    preset = session.query(models.SettingPreset).filter_by(is_active=True).first()
    if preset is None:
        raise RuntimeError("アクティブなプリセットがDBに存在しません")
    return preset
