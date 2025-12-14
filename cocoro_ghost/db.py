"""DB 接続とセッション管理（設定DB・記憶DB分離版）。"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import re
import time
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from cocoro_ghost.defaults import DEFAULT_EXCLUDE_KEYWORDS_JSON

logger = logging.getLogger(__name__)

# sqlite-vec 仮想テーブル名（検索用ベクトルインデックス）
# vec_units は本文を置かず unit_id で JOIN して取得する。
VEC_UNITS_TABLE_NAME = "vec_units"

# FTS5 仮想テーブル名（BM25インデックス）
EPISODE_FTS_TABLE_NAME = "episode_fts"

# 設定DB用 Base（GlobalSettings, LlmPreset, EmbeddingPreset）
Base = declarative_base()

# 記憶DB用 Base（Unit/payload/entities/jobs 等：新仕様）
UnitBase = declarative_base()

# グローバルセッション（設定DB用）
SettingsSessionLocal: sessionmaker | None = None

# 記憶DBセッションのキャッシュ（memory_id -> sessionmaker）
@dataclasses.dataclass(frozen=True)
class _MemorySessionEntry:
    session_factory: sessionmaker
    embedding_dimension: int


_memory_sessions: dict[str, _MemorySessionEntry] = {}


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
        vec_path = getattr(sqlite_vec, "loadable_path", None)
        vec_path = vec_path() if callable(vec_path) else str(Path(sqlite_vec.__file__).parent / "vec0")
        vec_path = str(vec_path)

        @event.listens_for(engine, "connect")
        def load_sqlite_vec_extension(dbapi_conn, connection_record):
            dbapi_conn.enable_load_extension(True)
            try:
                dbapi_conn.load_extension(vec_path)
            except Exception as exc:
                logger.error("sqlite-vec拡張のロードに失敗しました", exc_info=exc)
                raise

            # 接続ごとに必要なPRAGMAを適用（foreign_keysは接続ごとに有効化が必要）
            try:
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
                dbapi_conn.execute("PRAGMA synchronous=NORMAL")
                dbapi_conn.execute("PRAGMA temp_store=MEMORY")
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite PRAGMAの適用に失敗しました", exc_info=exc)

    return engine


def _enable_sqlite_vec(engine, dimension: int) -> None:
    """sqlite-vec の仮想テーブルを作成。sqlite-vec拡張は接続時に自動ロードされる。"""
    with engine.connect() as conn:
        existing = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name = :name"
            ),
            {"name": VEC_UNITS_TABLE_NAME},
        ).fetchone()
        if existing is not None and existing[0]:
            m = re.search(r"embedding\s+float\[(\d+)\]", str(existing[0]))
            if m:
                found = int(m.group(1))
                if found != int(dimension):
                    raise RuntimeError(
                        f"{VEC_UNITS_TABLE_NAME} embedding dimension mismatch: db={found}, expected={dimension}. "
                        "次元数を変える場合は別DBへ移行/再構築してください。"
                    )
            else:
                logger.warning("vec_units schema parse failed", extra={"sql": str(existing[0])})
        conn.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_UNITS_TABLE_NAME} USING vec0("
                f"unit_id integer primary key, "
                f"embedding float[{dimension}] distance_metric=cosine, "
                f"kind integer partition key, "
                f"occurred_day integer, "
                f"state integer, "
                f"sensitivity integer"
                f")"
            )
        )
        conn.commit()


def _enable_episode_fts(engine) -> None:
    """Episode の BM25 検索用に FTS5 仮想テーブルと同期トリガーを用意する。"""
    with engine.connect() as conn:
        existed = (
            conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": EPISODE_FTS_TABLE_NAME},
            ).fetchone()
            is not None
        )

        conn.execute(
            text(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {EPISODE_FTS_TABLE_NAME} USING fts5(
                    user_text,
                    reply_text,
                    content='payload_episode',
                    content_rowid='unit_id',
                    tokenize='unicode61'
                )
                """
            )
        )

        # external content FTS はトリガーで追従させる
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EPISODE_FTS_TABLE_NAME}_ai
                AFTER INSERT ON payload_episode
                BEGIN
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}(rowid, user_text, reply_text)
                    VALUES (new.unit_id, new.user_text, new.reply_text);
                END;
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EPISODE_FTS_TABLE_NAME}_ad
                AFTER DELETE ON payload_episode
                BEGIN
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}({EPISODE_FTS_TABLE_NAME}, rowid, user_text, reply_text)
                    VALUES ('delete', old.unit_id, old.user_text, old.reply_text);
                END;
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EPISODE_FTS_TABLE_NAME}_au
                AFTER UPDATE ON payload_episode
                BEGIN
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}({EPISODE_FTS_TABLE_NAME}, rowid, user_text, reply_text)
                    VALUES ('delete', old.unit_id, old.user_text, old.reply_text);
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}(rowid, user_text, reply_text)
                    VALUES (new.unit_id, new.user_text, new.reply_text);
                END;
                """
            )
        )

        if not existed:
            # 初回作成時のみ rebuild（既存の payload_episode を索引化）
            conn.execute(text(f"INSERT INTO {EPISODE_FTS_TABLE_NAME}({EPISODE_FTS_TABLE_NAME}) VALUES ('rebuild')"))

        conn.commit()


def _apply_memory_pragmas(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.execute(text("PRAGMA temp_store=MEMORY"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()


def _create_memory_indexes(engine) -> None:
    stmts = [
        "CREATE INDEX IF NOT EXISTS idx_units_kind_created ON units(kind, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_units_occurred ON units(occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_units_state ON units(state)",
        "CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(etype, name)",
        "CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias)",
        "CREATE INDEX IF NOT EXISTS idx_unit_entities_entity ON unit_entities(entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_subject_pred ON payload_fact(subject_entity_id, predicate)",
        "CREATE INDEX IF NOT EXISTS idx_summary_scope ON payload_summary(scope_type, scope_key)",
        "CREATE INDEX IF NOT EXISTS idx_loop_status_due ON payload_loop(status, due_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_run_after ON jobs(status, run_after)",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
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
    entry = _memory_sessions.get(memory_id)
    if entry is not None:
        if int(entry.embedding_dimension) != int(embedding_dimension):
            raise RuntimeError(
                f"memory_id={memory_id} は既に embedding_dimension={entry.embedding_dimension} で初期化済みです。"
                f"要求された embedding_dimension={embedding_dimension} とは一致しません。"
                "次元数を変える場合は別memory_idを使うかDBを再構築してください。"
            )
        return entry.session_factory

    db_url = get_memory_db_path(memory_id)
    engine = _create_engine_with_vec_support(db_url)

    _apply_memory_pragmas(engine)

    # 記憶用テーブルを作成（Unitベース新仕様）
    import cocoro_ghost.unit_models  # noqa: F401

    UnitBase.metadata.create_all(bind=engine)
    _create_memory_indexes(engine)
    _enable_episode_fts(engine)

    # sqlite-vec拡張を有効化
    if db_url.startswith("sqlite"):
        _enable_sqlite_vec(engine, embedding_dimension)

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    _memory_sessions[memory_id] = _MemorySessionEntry(session_factory=session_factory, embedding_dimension=int(embedding_dimension))
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


def upsert_unit_vector(
    session: Session,
    *,
    unit_id: int,
    embedding: list[float],
    kind: int,
    occurred_at: int | None,
    state: int,
    sensitivity: int,
) -> None:
    """Unitの検索用ベクトルを更新または挿入（sqlite-vec仮想テーブル）。"""
    embedding_json = json.dumps(embedding)
    occurred_day = (occurred_at // 86400) if occurred_at is not None else None
    session.execute(text(f"DELETE FROM {VEC_UNITS_TABLE_NAME} WHERE unit_id = :unit_id"), {"unit_id": unit_id})
    session.execute(
        text(
            f"""
            INSERT INTO {VEC_UNITS_TABLE_NAME}(unit_id, embedding, kind, occurred_day, state, sensitivity)
            VALUES (:unit_id, :embedding, :kind, :occurred_day, :state, :sensitivity)
            """
        ),
        {
            "unit_id": unit_id,
            "embedding": embedding_json,
            "kind": kind,
            "occurred_day": occurred_day,
            "state": state,
            "sensitivity": sensitivity,
        },
    )


def sync_unit_vector_metadata(
    session: Session,
    *,
    unit_id: int,
    occurred_at: int | None,
    state: int,
    sensitivity: int,
) -> None:
    """vec_units の metadata columns を units と同期する（埋め込みは更新しない）。"""
    occurred_day = (occurred_at // 86400) if occurred_at is not None else None
    session.execute(
        text(
            f"""
            UPDATE {VEC_UNITS_TABLE_NAME}
               SET occurred_day = :occurred_day,
                   state = :state,
                   sensitivity = :sensitivity
             WHERE unit_id = :unit_id
            """
        ),
        {
            "unit_id": unit_id,
            "occurred_day": occurred_day,
            "state": state,
            "sensitivity": sensitivity,
        },
    )


def search_similar_unit_ids(
    session: Session,
    *,
    query_embedding: list[float],
    k: int,
    kind: int,
    max_sensitivity: int,
    occurred_day_range: tuple[int, int] | None = None,
) -> list:
    """類似Unit IDを検索（sqlite-vec仮想テーブル）。"""
    query_json = json.dumps(query_embedding)
    day_filter = ""
    params = {
        "query": query_json,
        "k": k,
        "kind": kind,
        "max_sensitivity": max_sensitivity,
    }
    if occurred_day_range is not None:
        day_filter = "AND occurred_day BETWEEN :d0 AND :d1"
        params["d0"] = occurred_day_range[0]
        params["d1"] = occurred_day_range[1]

    rows = session.execute(
        text(
            f"""
            SELECT unit_id, distance
            FROM {VEC_UNITS_TABLE_NAME}
            WHERE embedding MATCH :query
              AND k = :k
              AND kind = :kind
              AND state IN (0, 1, 2)
              AND sensitivity <= :max_sensitivity
              {day_filter}
            ORDER BY distance ASC
            """
        ),
        params,
    ).fetchall()
    return list(rows)


# --- 初期設定作成 ---


def ensure_initial_settings(session: Session, toml_config) -> None:
    """設定DBに必要な初期レコードが無ければ作成する。"""
    from cocoro_ghost import models
    from cocoro_ghost import prompts

    # 既にアクティブなプリセットがあれば何もしない
    global_settings = session.query(models.GlobalSettings).first()
    if global_settings is not None and getattr(global_settings, "token", ""):
        ids = [
            global_settings.active_llm_preset_id,
            global_settings.active_embedding_preset_id,
            global_settings.active_persona_preset_id,
            global_settings.active_contract_preset_id,
        ]
        if all(x is not None for x in ids):
            active_llm = session.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id, archived=False).first()
            active_embedding = session.query(models.EmbeddingPreset).filter_by(
                id=global_settings.active_embedding_preset_id, archived=False
            ).first()
            active_persona = session.query(models.PersonaPreset).filter_by(
                id=global_settings.active_persona_preset_id, archived=False
            ).first()
            active_contract = session.query(models.ContractPreset).filter_by(
                id=global_settings.active_contract_preset_id, archived=False
            ).first()
            if active_llm and active_embedding and active_persona and active_contract:
                return

    logger.info("設定DBの初期化を行います（TOMLのLLM設定は使用しません）")

    # GlobalSettingsの用意
    if global_settings is None:
        global_settings = models.GlobalSettings(
            token=toml_config.token,
            exclude_keywords=DEFAULT_EXCLUDE_KEYWORDS_JSON,
        )
        session.add(global_settings)
        session.flush()
    elif not global_settings.exclude_keywords:
        global_settings.exclude_keywords = DEFAULT_EXCLUDE_KEYWORDS_JSON
    if not getattr(global_settings, "token", ""):
        global_settings.token = toml_config.token

    # LlmPresetの用意（存在しない/アクティブでない場合は空のdefaultを作成）
    llm_preset = None
    active_llm_id = global_settings.active_llm_preset_id
    if active_llm_id is not None:
        llm_preset = session.query(models.LlmPreset).filter_by(id=active_llm_id, archived=False).first()
    if llm_preset is None:
        llm_preset = session.query(models.LlmPreset).filter_by(archived=False).first()
    if llm_preset is None:
        logger.warning("LLMプリセットが無いため、空の default プリセットを作成します")
        llm_preset = models.LlmPreset(
            name="miku-default-llm",
            archived=False,
            llm_api_key="",
            llm_model="openai/gpt-5-mini",
            image_model="openai/gpt-5-mini",
            image_timeout_seconds=60,
        )
        session.add(llm_preset)
        session.flush()

    if active_llm_id is None or str(llm_preset.id) != str(active_llm_id):
        global_settings.active_llm_preset_id = str(llm_preset.id)

    # EmbeddingPreset の用意（存在しない/アクティブでない場合は default を作成）
    embedding_preset = None
    active_embedding_id = getattr(global_settings, "active_embedding_preset_id", None)
    if active_embedding_id is not None:
        embedding_preset = session.query(models.EmbeddingPreset).filter_by(id=active_embedding_id, archived=False).first()
    if embedding_preset is None:
        embedding_preset = session.query(models.EmbeddingPreset).filter_by(archived=False).first()
    if embedding_preset is None:
        embedding_preset = models.EmbeddingPreset(
            name="miku-default-emmbedding",
            archived=False,
            embedding_model="openai/text-embedding-3-large",
            embedding_api_key=None,
            embedding_base_url=None,
            embedding_dimension=3072,
            similar_episodes_limit=5,
            max_inject_tokens=1200,
            similar_limit_by_kind_json="{}",
        )
        session.add(embedding_preset)
        session.flush()

    if active_embedding_id is None or str(embedding_preset.id) != str(active_embedding_id):
        global_settings.active_embedding_preset_id = str(embedding_preset.id)

    # PersonaPreset の用意
    persona_preset = None
    active_persona_id = global_settings.active_persona_preset_id
    if active_persona_id is not None:
        persona_preset = session.query(models.PersonaPreset).filter_by(id=active_persona_id, archived=False).first()
    if persona_preset is None:
        persona_preset = session.query(models.PersonaPreset).filter_by(archived=False).first()
    if persona_preset is None:
        persona_preset = models.PersonaPreset(
            name="miku-default-persona_prompt",
            archived=False,
            persona_text=prompts.get_default_persona_anchor(),
        )
        session.add(persona_preset)
        session.flush()
    if active_persona_id is None or str(persona_preset.id) != str(active_persona_id):
        global_settings.active_persona_preset_id = str(persona_preset.id)

    # ContractPreset の用意
    contract_preset = None
    active_contract_id = global_settings.active_contract_preset_id
    if active_contract_id is not None:
        contract_preset = session.query(models.ContractPreset).filter_by(id=active_contract_id, archived=False).first()
    if contract_preset is None:
        contract_preset = session.query(models.ContractPreset).filter_by(archived=False).first()
    if contract_preset is None:
        contract_preset = models.ContractPreset(
            name="miku-default-contract_prompt",
            archived=False,
            contract_text=prompts.get_default_relationship_contract(),
        )
        session.add(contract_preset)
        session.flush()
    if active_contract_id is None or str(contract_preset.id) != str(active_contract_id):
        global_settings.active_contract_preset_id = str(contract_preset.id)

    session.commit()


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

    preset = session.query(models.LlmPreset).filter_by(id=settings.active_llm_preset_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"LLMプリセット(id={settings.active_llm_preset_id})が存在しません")
    return preset


def load_active_embedding_preset(session: Session):
    """アクティブなEmbeddingPresetを取得。"""
    from cocoro_ghost import models

    settings = load_global_settings(session)
    active_id = getattr(settings, "active_embedding_preset_id", None)
    if active_id is None:
        raise RuntimeError("アクティブなEmbeddingプリセットが設定されていません")

    preset = session.query(models.EmbeddingPreset).filter_by(id=active_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"Embeddingプリセット(id={active_id})が存在しません")
    return preset


def load_active_persona_preset(session: Session):
    """アクティブなPersonaPresetを取得。"""
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_persona_preset_id is None:
        raise RuntimeError("アクティブなpersonaプリセットが設定されていません")

    preset = session.query(models.PersonaPreset).filter_by(id=settings.active_persona_preset_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"PersonaPreset(id={settings.active_persona_preset_id})が存在しません")
    return preset


def load_active_contract_preset(session: Session):
    """アクティブなContractPresetを取得。"""
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_contract_preset_id is None:
        raise RuntimeError("アクティブなcontractプリセットが設定されていません")

    preset = session.query(models.ContractPreset).filter_by(id=settings.active_contract_preset_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"ContractPreset(id={settings.active_contract_preset_id})が存在しません")
    return preset
