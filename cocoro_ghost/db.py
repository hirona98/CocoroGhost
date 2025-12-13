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
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

logger = logging.getLogger(__name__)

# sqlite-vec 仮想テーブル名（検索用ベクトルインデックス）
# vec_units は本文を置かず unit_id で JOIN して取得する。
VEC_UNITS_TABLE_NAME = "vec_units"

# 設定DB用 Base（GlobalSettings, LlmPreset, CharacterPreset, 旧SettingPreset）
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


def _ensure_default_persona_contract(session: Session) -> None:
    from cocoro_ghost import prompts
    from cocoro_ghost.unit_enums import Sensitivity, UnitKind, UnitState
    from cocoro_ghost.unit_models import PayloadContract, PayloadPersona, Unit

    now_ts = int(time.time())

    has_persona = (
        session.query(PayloadPersona.unit_id)
        .join(Unit, Unit.id == PayloadPersona.unit_id)
        .filter(Unit.kind == int(UnitKind.PERSONA), PayloadPersona.is_active == 1)
        .limit(1)
        .scalar()
    )
    if not has_persona:
        unit = Unit(
            kind=int(UnitKind.PERSONA),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source="seed",
            state=int(UnitState.RAW),
            confidence=0.5,
            salience=0.0,
            sensitivity=int(Sensitivity.NORMAL),
            pin=1,
        )
        session.add(unit)
        session.flush()
        session.add(PayloadPersona(unit_id=unit.id, persona_text=prompts.get_default_persona_anchor(), is_active=1))

    has_contract = (
        session.query(PayloadContract.unit_id)
        .join(Unit, Unit.id == PayloadContract.unit_id)
        .filter(Unit.kind == int(UnitKind.CONTRACT), PayloadContract.is_active == 1)
        .limit(1)
        .scalar()
    )
    if not has_contract:
        unit = Unit(
            kind=int(UnitKind.CONTRACT),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source="seed",
            state=int(UnitState.RAW),
            confidence=0.5,
            salience=0.0,
            sensitivity=int(Sensitivity.NORMAL),
            pin=1,
        )
        session.add(unit)
        session.flush()
        session.add(
            PayloadContract(unit_id=unit.id, contract_text=prompts.get_default_relationship_contract(), is_active=1)
        )


# --- 設定DB ---


def init_settings_db() -> None:
    """設定DBを初期化。"""
    global SettingsSessionLocal

    db_url = get_settings_db_path()
    connect_args = {"check_same_thread": False}
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    SettingsSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)

    # 既存DB向けの軽量アップグレード（運用していない前提でも、開発中に古いDBが残りやすい）
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE global_settings ADD COLUMN token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE global_settings ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE llm_presets ADD COLUMN max_inject_tokens INTEGER NOT NULL DEFAULT 1200",
            "ALTER TABLE llm_presets ADD COLUMN similar_limit_by_kind_json TEXT NOT NULL DEFAULT '{}'",
        ]:
            try:
                conn.execute(text(stmt))
            except OperationalError:
                # 既にカラムが存在する場合など
                pass
        conn.commit()
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

    # sqlite-vec拡張を有効化
    if db_url.startswith("sqlite"):
        _enable_sqlite_vec(engine, embedding_dimension)

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    _memory_sessions[memory_id] = _MemorySessionEntry(session_factory=session_factory, embedding_dimension=int(embedding_dimension))
    session = session_factory()
    try:
        _ensure_default_persona_contract(session)
        session.commit()
    finally:
        session.close()
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
    from cocoro_ghost.prompts import CHARACTER_SYSTEM_PROMPT

    # 既にアクティブなプリセットがあれば何もしない
    global_settings = session.query(models.GlobalSettings).first()
    if (
        global_settings is not None
        and getattr(global_settings, "token", "")
        and global_settings.active_llm_preset_id is not None
        and global_settings.active_character_preset_id is not None
    ):
        return

    logger.info("設定DBの初期化を行います（TOMLのLLM設定は使用しません）")

    # GlobalSettingsの用意
    if global_settings is None:
        global_settings = models.GlobalSettings(
            token=toml_config.token,
            exclude_keywords=json.dumps(toml_config.exclude_keywords or [])
        )
        session.add(global_settings)
        session.flush()
    elif not global_settings.exclude_keywords:
        global_settings.exclude_keywords = json.dumps(toml_config.exclude_keywords or [])
    if not getattr(global_settings, "token", ""):
        global_settings.token = toml_config.token

    # LlmPresetの用意（存在しない/アクティブでない場合は空のdefaultを作成）
    llm_preset = None
    if global_settings.active_llm_preset_id is not None:
        llm_preset = (
            session.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id).first()
        )
    if llm_preset is None:
        llm_preset = session.query(models.LlmPreset).first()
    if llm_preset is None:
        logger.warning("LLMプリセットが無いため、空の default プリセットを作成します")
        llm_preset = models.LlmPreset(
            name="default",
            llm_api_key="",
            llm_model="unset",
            embedding_model="unset",
            embedding_api_key=None,
            embedding_dimension=toml_config.embedding_dimension,
            image_model="unset",
            image_timeout_seconds=toml_config.image_timeout_seconds,
            similar_episodes_limit=toml_config.similar_episodes_limit,
            max_inject_tokens=1200,
            similar_limit_by_kind_json="{}",
        )
        session.add(llm_preset)
        session.flush()

    # CharacterPresetの用意（存在しない/アクティブでない場合はdefaultを作成）
    char_preset = None
    if global_settings.active_character_preset_id is not None:
        char_preset = (
            session.query(models.CharacterPreset).filter_by(id=global_settings.active_character_preset_id).first()
        )
    if char_preset is None:
        char_preset = session.query(models.CharacterPreset).first()
    if char_preset is None:
        char_preset = models.CharacterPreset(
            name="default",
            system_prompt=toml_config.character_prompt or CHARACTER_SYSTEM_PROMPT,
            memory_id="default",
        )
        session.add(char_preset)
        session.flush()

    if global_settings.active_llm_preset_id is None:
        global_settings.active_llm_preset_id = llm_preset.id
    if global_settings.active_character_preset_id is None:
        global_settings.active_character_preset_id = char_preset.id

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


