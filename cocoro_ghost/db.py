"""DB 接続とセッション管理（設定DB・記憶DB分離版）。"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

logger = logging.getLogger(__name__)

# 設定DB用 Base（GlobalSettings, LlmPreset, CharacterPreset, 旧SettingPreset）
Base = declarative_base()

# 記憶DB用 Base（Episode, Person, EpisodePerson）
MemoryBase = declarative_base()

# グローバルセッション（設定DB用）
SettingsSessionLocal: sessionmaker | None = None

# 記憶DBセッションのキャッシュ（memory_id -> sessionmaker）
_memory_sessions: dict[str, sessionmaker] = {}


def get_data_dir() -> Path:
    """データディレクトリを取得（存在しなければ作成）。"""
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


def get_settings_db_path() -> str:
    """設定DBのパスを取得。"""
    return f"sqlite:///{get_data_dir() / 'settings.db'}"


def get_memory_db_path(memory_id: str) -> str:
    """記憶DBのパスを取得。"""
    return f"sqlite:///{get_data_dir() / f'memory_{memory_id}.db'}"


def _create_engine_with_vec_support(db_url: str):
    """sqlite-vec拡張をサポートするエンジンを作成。"""
    import sqlite_vec

    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, future=True, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        vec_path = str(Path(sqlite_vec.__file__).parent / "vec0")

        @event.listens_for(engine, "connect")
        def load_sqlite_vec_extension(dbapi_conn, connection_record):
            dbapi_conn.enable_load_extension(True)
            try:
                dbapi_conn.load_extension(vec_path)
            except Exception as exc:
                logger.error("sqlite-vec拡張のロードに失敗しました", exc_info=exc)
                raise

    return engine


def _enable_sqlite_vec(engine, dimension: int) -> None:
    """episode_embeddings仮想テーブルを作成。sqlite-vec拡張は接続時に自動ロードされる。"""
    with engine.connect() as conn:
        conn.execute(
            text(f"CREATE VIRTUAL TABLE IF NOT EXISTS episode_embeddings USING vec0(embedding float[{dimension}])")
        )
        conn.commit()


# --- 設定DB ---


def init_settings_db() -> None:
    """設定DBを初期化。"""
    global SettingsSessionLocal

    db_url = get_settings_db_path()
    connect_args = {"check_same_thread": False}
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    SettingsSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    logger.info(f"設定DB初期化完了: {db_url}")


def get_settings_db() -> Iterator[Session]:
    """設定DBのセッションを取得（FastAPI依存性注入用）。"""
    if SettingsSessionLocal is None:
        raise RuntimeError("Settings database not initialized. Call init_settings_db() first.")
    db = SettingsSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def settings_session_scope() -> Iterator[Session]:
    """設定DBのセッションスコープ。"""
    if SettingsSessionLocal is None:
        raise RuntimeError("Settings database not initialized. Call init_settings_db() first.")
    session = SettingsSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- 記憶DB ---


def init_memory_db(memory_id: str, embedding_dimension: int) -> sessionmaker:
    """指定されたmemory_idの記憶DBを初期化し、sessionmakerを返す。"""
    if memory_id in _memory_sessions:
        return _memory_sessions[memory_id]

    db_url = get_memory_db_path(memory_id)
    engine = _create_engine_with_vec_support(db_url)

    # 記憶用テーブルを作成
    MemoryBase.metadata.create_all(bind=engine)

    # sqlite-vec拡張を有効化
    if db_url.startswith("sqlite"):
        _enable_sqlite_vec(engine, embedding_dimension)

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    _memory_sessions[memory_id] = session_factory
    logger.info(f"記憶DB初期化完了: {db_url}")
    return session_factory


def get_memory_session(memory_id: str, embedding_dimension: int) -> Session:
    """指定されたmemory_idの記憶DBセッションを取得。"""
    session_factory = init_memory_db(memory_id, embedding_dimension)
    return session_factory()


@contextlib.contextmanager
def memory_session_scope(memory_id: str, embedding_dimension: int) -> Iterator[Session]:
    """記憶DBのセッションスコープ。"""
    session = get_memory_session(memory_id, embedding_dimension)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- 埋め込みベクトル操作 ---


def upsert_episode_embedding(session: Session, episode_id: int, embedding: list[float]) -> None:
    """エピソードの埋め込みベクトルを更新または挿入。"""
    embedding_json = json.dumps(embedding)
    session.execute(text("DELETE FROM episode_embeddings WHERE rowid = :episode_id"), {"episode_id": episode_id})
    session.execute(
        text("INSERT INTO episode_embeddings(rowid, embedding) VALUES (:episode_id, :embedding)"),
        {"episode_id": episode_id, "embedding": embedding_json},
    )


def search_similar_episodes(session: Session, query_embedding: list[float], limit: int = 5):
    """類似エピソードを検索。"""
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


# --- マイグレーション ---


def migrate_toml_to_v2_if_needed(session: Session, toml_config) -> None:
    """TOMLからv2テーブルへの初回マイグレーション（データが空の場合）。"""
    from cocoro_ghost import models
    from cocoro_ghost.prompts import CHARACTER_SYSTEM_PROMPT

    # 既にGlobalSettingsがあればスキップ
    global_settings = session.query(models.GlobalSettings).first()
    if global_settings is not None:
        return

    # TOMLから新規作成
    if not toml_config.llm_api_key or not toml_config.llm_model:
        logger.warning("TOMLにLLM設定が無いため、空のGlobalSettingsのみ作成します")
        global_settings = models.GlobalSettings(
            exclude_keywords=json.dumps(toml_config.exclude_keywords or [])
        )
        session.add(global_settings)
        session.commit()
        return

    logger.info("TOML設定からv2テーブルを初期化します")

    # LlmPreset作成
    llm_preset = models.LlmPreset(
        name="default",
        llm_api_key=toml_config.llm_api_key,
        llm_model=toml_config.llm_model,
        embedding_model=toml_config.embedding_model,
        embedding_api_key=toml_config.llm_api_key,
        embedding_dimension=toml_config.embedding_dimension,
        image_model=toml_config.image_model,
        image_timeout_seconds=toml_config.image_timeout_seconds,
        similar_episodes_limit=toml_config.similar_episodes_limit,
    )
    session.add(llm_preset)
    session.flush()

    # CharacterPreset作成
    char_preset = models.CharacterPreset(
        name="default",
        system_prompt=toml_config.character_prompt or CHARACTER_SYSTEM_PROMPT,
        memory_id="default",
    )
    session.add(char_preset)
    session.flush()

    # GlobalSettings作成
    global_settings = models.GlobalSettings(
        exclude_keywords=json.dumps(toml_config.exclude_keywords or []),
        active_llm_preset_id=llm_preset.id,
        active_character_preset_id=char_preset.id,
    )
    session.add(global_settings)
    session.commit()

    logger.info("TOML -> v2 マイグレーション完了")


def load_global_settings(session: Session):
    """GlobalSettingsを取得。"""
    from cocoro_ghost import models

    settings = session.query(models.GlobalSettings).first()
    if settings is None:
        raise RuntimeError("GlobalSettingsがDBに存在しません")
    return settings


def load_active_llm_preset(session: Session):
    """アクティブなLlmPresetを取得。"""
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_llm_preset_id is None:
        raise RuntimeError("アクティブなLLMプリセットが設定されていません")

    preset = session.query(models.LlmPreset).filter_by(id=settings.active_llm_preset_id).first()
    if preset is None:
        raise RuntimeError(f"LLMプリセット(id={settings.active_llm_preset_id})が存在しません")
    return preset


def load_active_character_preset(session: Session):
    """アクティブなCharacterPresetを取得。"""
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_character_preset_id is None:
        raise RuntimeError("アクティブなキャラクタープリセットが設定されていません")

    preset = session.query(models.CharacterPreset).filter_by(id=settings.active_character_preset_id).first()
    if preset is None:
        raise RuntimeError(f"キャラクタープリセット(id={settings.active_character_preset_id})が存在しません")
    return preset


