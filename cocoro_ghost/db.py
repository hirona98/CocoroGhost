"""
DB 接続とセッション管理（設定DB・記憶DB分離版）

SQLite+sqlite-vecを使用したデータベース管理モジュール。
設定DBと記憶DBを分離し、それぞれ独立して管理する。
ベクトル検索（sqlite-vec）とBM25検索（FTS5）をサポート。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import re
import time
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, func, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from cocoro_ghost.defaults import DEFAULT_EXCLUDE_KEYWORDS_JSON

logger = logging.getLogger(__name__)

# sqlite-vec 仮想テーブル名（検索用ベクトルインデックス）
# vec_units は本文を置かず unit_id で JOIN して取得する
VEC_UNITS_TABLE_NAME = "vec_units"

# FTS5 仮想テーブル名（BM25インデックス）
EPISODE_FTS_TABLE_NAME = "episode_fts"

# 設定DB用 Base（GlobalSettings, LlmPreset, EmbeddingPreset）
Base = declarative_base()

# 記憶DB用 Base（Unit/payload/entities/jobs 等：新仕様）
UnitBase = declarative_base()

# グローバルセッション（設定DB用）
SettingsSessionLocal: sessionmaker | None = None


@dataclasses.dataclass(frozen=True)
class _MemorySessionEntry:
    """記憶DBセッションのエントリ（セッションファクトリと次元数を保持）。"""
    session_factory: sessionmaker
    embedding_dimension: int


# 記憶DBセッションのキャッシュ（embedding_preset_id -> sessionmaker）
_memory_sessions: dict[str, _MemorySessionEntry] = {}


def get_data_dir() -> Path:
    """
    データディレクトリを取得する。
    存在しなければ作成する。
    """
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


def get_settings_db_path() -> str:
    """設定DBのパスを取得。SQLAlchemy用のURL形式で返す。"""
    return f"sqlite:///{get_data_dir() / 'settings.db'}"


def get_memory_db_path(embedding_preset_id: str) -> str:
    """記憶DBのパスを取得。embedding_preset_idごとに別ファイルとなる。"""
    return f"sqlite:///{get_data_dir() / f'memory_{embedding_preset_id}.db'}"


def _create_engine_with_vec_support(db_url: str):
    """
    sqlite-vec拡張をサポートするSQLAlchemyエンジンを作成する。
    接続ごとに拡張をロードし、必要なPRAGMAを適用する。
    """
    import sqlite_vec

    # SQLiteの場合はスレッドチェックを無効化し、ロック解消を待つ。
    connect_args = {"check_same_thread": False, "timeout": 10.0} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, future=True, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        # sqlite-vec拡張のパスを取得
        vec_path = getattr(sqlite_vec, "loadable_path", None)
        vec_path = vec_path() if callable(vec_path) else str(Path(sqlite_vec.__file__).parent / "vec0")
        vec_path = str(vec_path)

        @event.listens_for(engine, "connect")
        def load_sqlite_vec_extension(dbapi_conn, connection_record):
            """SQLite接続ごとにsqlite-vec拡張をロードし、必要PRAGMAを適用する。"""
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
    """
    sqlite-vecの仮想テーブルを作成する。
    既存テーブルがある場合は次元数の一致を確認する。
    """
    with engine.connect() as conn:
        # 既存テーブルの確認
        existing = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name = :name"
            ),
            {"name": VEC_UNITS_TABLE_NAME},
        ).fetchone()

        # 次元数の一致確認
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

        # ベクトル検索用仮想テーブルを作成
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
    """
    EpisodeのBM25検索用にFTS5仮想テーブルと同期トリガーを用意する。
    payload_episodeテーブルの変更に自動追従する。
    """
    with engine.connect() as conn:
        # FTSテーブルの存在確認
        existed = (
            conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": EPISODE_FTS_TABLE_NAME},
            ).fetchone()
            is not None
        )

        # FTS5仮想テーブル作成
        conn.execute(
            text(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {EPISODE_FTS_TABLE_NAME} USING fts5(
                    input_text,
                    reply_text,
                    content='payload_episode',
                    content_rowid='unit_id',
                    tokenize='unicode61'
                )
                """
            )
        )

        # external content FTS はトリガーで追従させる
        # INSERT用トリガー
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EPISODE_FTS_TABLE_NAME}_ai
                AFTER INSERT ON payload_episode
                BEGIN
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}(rowid, input_text, reply_text)
                    VALUES (new.unit_id, new.input_text, new.reply_text);
                END;
                """
            )
        )
        # DELETE用トリガー
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EPISODE_FTS_TABLE_NAME}_ad
                AFTER DELETE ON payload_episode
                BEGIN
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}({EPISODE_FTS_TABLE_NAME}, rowid, input_text, reply_text)
                    VALUES ('delete', old.unit_id, old.input_text, old.reply_text);
                END;
                """
            )
        )
        # UPDATE用トリガー（DELETE + INSERT）
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EPISODE_FTS_TABLE_NAME}_au
                AFTER UPDATE ON payload_episode
                BEGIN
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}({EPISODE_FTS_TABLE_NAME}, rowid, input_text, reply_text)
                    VALUES ('delete', old.unit_id, old.input_text, old.reply_text);
                    INSERT INTO {EPISODE_FTS_TABLE_NAME}(rowid, input_text, reply_text)
                    VALUES (new.unit_id, new.input_text, new.reply_text);
                END;
                """
            )
        )

        # 初回作成時のみ rebuild（既存の payload_episode を索引化）
        if not existed:
            conn.execute(text(f"INSERT INTO {EPISODE_FTS_TABLE_NAME}({EPISODE_FTS_TABLE_NAME}) VALUES ('rebuild')"))

        conn.commit()


def _apply_memory_pragmas(engine) -> None:
    """記憶DBのパフォーマンス設定PRAGMAを適用する。"""
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))      # WALモードで並行性向上
        conn.execute(text("PRAGMA synchronous=NORMAL"))    # 書き込み性能最適化
        conn.execute(text("PRAGMA temp_store=MEMORY"))     # 一時テーブルをメモリに
        conn.execute(text("PRAGMA foreign_keys=ON"))       # 外部キー制約を有効化
        conn.commit()


def _create_memory_indexes(engine) -> None:
    """記憶DBの検索性能向上用インデックスを作成する。"""
    stmts = [
        "CREATE INDEX IF NOT EXISTS idx_units_kind_created ON units(kind, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_units_occurred ON units(occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_units_state ON units(state)",
        "CREATE INDEX IF NOT EXISTS idx_entities_label_name ON entities(type_label, name)",
        "CREATE INDEX IF NOT EXISTS idx_entities_normalized ON entities(normalized)",
        "CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias)",
        "CREATE INDEX IF NOT EXISTS idx_unit_entities_entity ON unit_entities(entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_subject_pred ON payload_fact(subject_entity_id, predicate)",
        "CREATE INDEX IF NOT EXISTS idx_summary_scope ON payload_summary(scope_label, scope_key)",
        # open loops は短期メモ（TTL）として扱い、loop_text で一意に扱う（重複抑制）。
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_loop_text_unique ON payload_loop(loop_text)",
        "CREATE INDEX IF NOT EXISTS idx_loop_due ON payload_loop(due_at)",
        "CREATE INDEX IF NOT EXISTS idx_loop_expires ON payload_loop(expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_run_after ON jobs(status, run_after)",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.commit()


# --- 設定DB ---


def init_settings_db() -> None:
    """
    設定DBを初期化する。
    グローバルセッションファクトリを作成し、テーブルを作成する。
    """
    global SettingsSessionLocal

    db_url = get_settings_db_path()
    # SQLiteのロック待ちは短いタイムアウトで十分。
    connect_args = {"check_same_thread": False, "timeout": 10.0}
    engine = create_engine(db_url, future=True, connect_args=connect_args)
    SettingsSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    logger.info(f"設定DB初期化完了: {db_url}")


def get_settings_db() -> Iterator[Session]:
    """
    設定DBのセッションを取得する（FastAPI依存性注入用）。
    使用後は自動的にクローズされる。
    """
    if SettingsSessionLocal is None:
        raise RuntimeError("Settings database not initialized. Call init_settings_db() first.")
    db = SettingsSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def settings_session_scope() -> Iterator[Session]:
    """
    設定DBのセッションスコープ（with文用）。
    正常終了時はコミット、例外時はロールバックする。
    """
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


def init_memory_db(embedding_preset_id: str, embedding_dimension: int) -> sessionmaker:
    """
    指定されたembedding_preset_idの記憶DBを初期化し、sessionmakerを返す。
    既に初期化済みの場合はキャッシュから返す。
    """
    # キャッシュ確認
    entry = _memory_sessions.get(embedding_preset_id)
    if entry is not None:
        # 次元数の一致確認
        if int(entry.embedding_dimension) != int(embedding_dimension):
            raise RuntimeError(
                f"embedding_preset_id={embedding_preset_id} は既に embedding_dimension={entry.embedding_dimension} で初期化済みです。"
                f"要求された embedding_dimension={embedding_dimension} とは一致しません。"
                "次元数を変える場合は別embedding_preset_idを使うかDBを再構築してください。"
            )
        return entry.session_factory

    db_url = get_memory_db_path(embedding_preset_id)
    engine = _create_engine_with_vec_support(db_url)

    # パフォーマンス設定を適用
    _apply_memory_pragmas(engine)

    # 記憶用テーブルを作成（Unitベース新仕様）
    import cocoro_ghost.unit_models  # noqa: F401

    UnitBase.metadata.create_all(bind=engine)
    _create_memory_indexes(engine)
    _enable_episode_fts(engine)

    # sqlite-vec拡張を有効化
    if db_url.startswith("sqlite"):
        _enable_sqlite_vec(engine, embedding_dimension)

    # セッションファクトリをキャッシュ
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    _memory_sessions[embedding_preset_id] = _MemorySessionEntry(
        session_factory=session_factory,
        embedding_dimension=int(embedding_dimension),
    )
    logger.info(f"記憶DB初期化完了: {db_url}")
    return session_factory


def get_memory_session(embedding_preset_id: str, embedding_dimension: int) -> Session:
    """指定されたembedding_preset_idの記憶DBセッションを取得する。"""
    session_factory = init_memory_db(embedding_preset_id, embedding_dimension)
    return session_factory()


def upsert_edges(session: Session, *, rows: list[dict]) -> None:
    """edges をUPSERTする。

    - edges は (src_entity_id, relation_label, dst_entity_id) が主キー。
    - Worker側が冪等に動けるのが本来あるべき姿なので、UNIQUE違反で落とさない。
    - 同一キーが既に存在する場合は、weight/first_seen_at/last_seen_at を自然に統合する。
    """
    if not rows:
        return

    # 循環importを避けるため、ここでimportする。
    from cocoro_ghost.unit_models import Edge

    stmt = sqlite_insert(Edge).values(rows)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[Edge.src_entity_id, Edge.relation_label, Edge.dst_entity_id],
        set_={
            # 強さは「強い方」を採用（過去情報を弱めて消さない）。
            "weight": func.max(Edge.weight, excluded.weight),
            # 初出は早い方 / 最終確認は遅い方。
            "first_seen_at": func.min(func.coalesce(Edge.first_seen_at, excluded.first_seen_at), excluded.first_seen_at),
            "last_seen_at": func.max(func.coalesce(Edge.last_seen_at, excluded.last_seen_at), excluded.last_seen_at),
            # evidence は最新のUnitに更新（単一列なので履歴は持てない）。
            "evidence_unit_id": excluded.evidence_unit_id,
        },
    )
    session.execute(stmt)


@contextlib.contextmanager
def memory_session_scope(embedding_preset_id: str, embedding_dimension: int) -> Iterator[Session]:
    """
    記憶DBのセッションスコープ（with文用）。
    正常終了時はコミット、例外時はロールバックする。
    """
    session = get_memory_session(embedding_preset_id, embedding_dimension)
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
    """
    Unitの検索用ベクトルを更新または挿入する（sqlite-vec仮想テーブル）。
    既存のベクトルがあれば削除してから挿入する。
    """
    embedding_json = json.dumps(embedding)
    # 日付をエポック日数に変換（検索フィルタ用）
    occurred_day = (occurred_at // 86400) if occurred_at is not None else None

    # 既存レコードを削除してから挿入
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


def delete_unit_vector(session: Session, *, unit_id: int) -> None:
    """
    Unitの検索用ベクトルを削除する（sqlite-vec仮想テーブル）。

    Unit本体の削除はFKカスケードで payload_* 等が消えるが、vec0の仮想テーブルは
    外部キーでカスケードできないため、明示的に削除する。
    """
    session.execute(text(f"DELETE FROM {VEC_UNITS_TABLE_NAME} WHERE unit_id = :unit_id"), {"unit_id": int(unit_id)})


def sync_unit_vector_metadata(
    session: Session,
    *,
    unit_id: int,
    occurred_at: int | None,
    state: int,
    sensitivity: int,
) -> None:
    """
    vec_unitsのメタデータカラムをunitsと同期する（埋め込みは更新しない）。
    状態変更や日付変更時にフィルタ条件を更新するために使用。
    """
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
    """
    類似Unit IDを検索する（sqlite-vec仮想テーブル）。
    コサイン距離に基づいてk件の類似ユニットを返す。
    """
    query_json = json.dumps(query_embedding)

    # 日付範囲フィルタの構築
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

    # ベクトル類似検索を実行
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
    """
    設定DBに必要な初期レコードが無ければ作成する。
    各種プリセット（LLM, Embedding, Persona, Addon）のデフォルトを用意する。
    """
    from cocoro_ghost import models
    from cocoro_ghost import prompts

    # 既にアクティブなプリセットがあれば何もしない
    global_settings = session.query(models.GlobalSettings).first()
    if global_settings is not None and getattr(global_settings, "token", ""):
        ids = [
            global_settings.active_llm_preset_id,
            global_settings.active_embedding_preset_id,
            global_settings.active_persona_preset_id,
            global_settings.active_addon_preset_id,
        ]
        # すべてのプリセットIDが設定済みか確認
        if all(x is not None for x in ids):
            active_llm = session.query(models.LlmPreset).filter_by(id=global_settings.active_llm_preset_id, archived=False).first()
            active_embedding = session.query(models.EmbeddingPreset).filter_by(
                id=global_settings.active_embedding_preset_id, archived=False
            ).first()
            active_persona = session.query(models.PersonaPreset).filter_by(
                id=global_settings.active_persona_preset_id, archived=False
            ).first()
            active_addon = session.query(models.AddonPreset).filter_by(
                id=global_settings.active_addon_preset_id, archived=False
            ).first()
            if active_llm and active_embedding and active_persona and active_addon:
                return

    logger.info("設定DBの初期化を行います（TOMLのLLM設定は使用しません）")

    # GlobalSettingsの用意
    if global_settings is None:
        global_settings = models.GlobalSettings(
            token=toml_config.token,
            exclude_keywords=DEFAULT_EXCLUDE_KEYWORDS_JSON,
            memory_enabled=True,
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

    # AddonPreset の用意（persona への任意追加オプション）
    addon_preset = None
    active_addon_id = global_settings.active_addon_preset_id
    if active_addon_id is not None:
        addon_preset = session.query(models.AddonPreset).filter_by(id=active_addon_id, archived=False).first()
    if addon_preset is None:
        addon_preset = session.query(models.AddonPreset).filter_by(archived=False).first()
    if addon_preset is None:
        addon_preset = models.AddonPreset(
            name="miku-default-addon_prompt",
            archived=False,
            addon_text=prompts.get_default_persona_addon(),
        )
        session.add(addon_preset)
        session.flush()
    if active_addon_id is None or str(addon_preset.id) != str(active_addon_id):
        global_settings.active_addon_preset_id = str(addon_preset.id)

    session.commit()


def load_global_settings(session: Session):
    """
    GlobalSettingsを取得する。
    存在しない場合はRuntimeErrorを発生させる。
    """
    from cocoro_ghost import models

    settings = session.query(models.GlobalSettings).first()
    if settings is None:
        raise RuntimeError("GlobalSettingsがDBに存在しません")
    return settings


def load_active_llm_preset(session: Session):
    """
    アクティブなLlmPresetを取得する。
    設定されていない場合はRuntimeErrorを発生させる。
    """
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_llm_preset_id is None:
        raise RuntimeError("アクティブなLLMプリセットが設定されていません")

    preset = session.query(models.LlmPreset).filter_by(id=settings.active_llm_preset_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"LLMプリセット(id={settings.active_llm_preset_id})が存在しません")
    return preset


def load_active_embedding_preset(session: Session):
    """
    アクティブなEmbeddingPresetを取得する。
    設定されていない場合はRuntimeErrorを発生させる。
    """
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
    """
    アクティブなPersonaPresetを取得する。
    設定されていない場合はRuntimeErrorを発生させる。
    """
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_persona_preset_id is None:
        raise RuntimeError("アクティブなpersonaプリセットが設定されていません")

    preset = session.query(models.PersonaPreset).filter_by(id=settings.active_persona_preset_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"PersonaPreset(id={settings.active_persona_preset_id})が存在しません")
    return preset


def load_active_addon_preset(session: Session):
    """
    アクティブなAddonPresetを取得する。
    設定されていない場合はRuntimeErrorを発生させる。
    """
    from cocoro_ghost import models

    settings = load_global_settings(session)
    if settings.active_addon_preset_id is None:
        raise RuntimeError("アクティブなaddonプリセットが設定されていません")

    preset = session.query(models.AddonPreset).filter_by(id=settings.active_addon_preset_id, archived=False).first()
    if preset is None:
        raise RuntimeError(f"AddonPreset(id={settings.active_addon_preset_id})が存在しません")
    return preset
