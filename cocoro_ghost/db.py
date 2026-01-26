"""
DB 接続とセッション管理（設定DB・記憶DB分離版）

SQLite+sqlite-vecを使用したデータベース管理モジュール。
設定DBと記憶DBを分離し、それぞれ独立して管理する。
ベクトル検索（sqlite-vec）と文字検索（FTS5 `trigram`）をサポートする。
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

logger = logging.getLogger(__name__)

# sqlite-vec 仮想テーブル名（検索用ベクトルインデックス）
VEC_ITEMS_TABLE_NAME = "vec_items"

# FTS5 仮想テーブル名（文字検索インデックス）
EVENTS_FTS_TABLE_NAME = "events_fts"

# 設定DB用 Base（GlobalSettings, LlmPreset, EmbeddingPreset）
Base = declarative_base()

# 記憶DB用 Base（events/state/revisions 等）
MemoryBase = declarative_base()

# グローバルセッション（設定DB用）
SettingsSessionLocal: sessionmaker | None = None


@dataclasses.dataclass(frozen=True)
class _MemorySessionEntry:
    """記憶DBセッションのエントリ（セッションファクトリと次元数を保持）。"""
    session_factory: sessionmaker
    embedding_dimension: int


# 記憶DBセッションのキャッシュ（embedding_preset_id -> sessionmaker）
_memory_sessions: dict[str, _MemorySessionEntry] = {}


_MEMORY_DB_USER_VERSION = 7
_SETTINGS_DB_USER_VERSION = 2


def get_db_dir() -> Path:
    """DB 保存先ディレクトリを取得する。

    - settings.db / memory_*.db の保存先を一元化する。
    - 配布（PyInstaller）では `..\\UserData\\Ghost\\` を使用する。
    """

    # --- パス解決は paths に集約する ---
    from cocoro_ghost.paths import get_db_dir as _get_db_dir

    return _get_db_dir()


def get_settings_db_path() -> str:
    """設定DBのパスを取得。SQLAlchemy用のURL形式で返す。"""

    db_path = (get_db_dir() / "settings.db").resolve()
    return f"sqlite:///{db_path}"


def get_memory_db_path(embedding_preset_id: str) -> str:
    """記憶DBのパスを取得。embedding_preset_idごとに別ファイルとなる。"""

    db_path = (get_db_dir() / f"memory_{embedding_preset_id}.db").resolve()
    return f"sqlite:///{db_path}"


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

        # --- PyInstaller ではバイナリの配置が変わることがあるためフォールバック探索 ---
        # 期待パスに実体が無い場合は、exe隣/パッケージ隣から vec0.* を探す。
        p = Path(vec_path)
        if not p.exists():
            try:
                from cocoro_ghost.paths import get_app_root_dir

                search_dirs = [get_app_root_dir(), Path(sqlite_vec.__file__).resolve().parent]
                candidates: list[Path] = []
                for d in search_dirs:
                    # Windows では vec0.dll が主。将来の拡張子差も吸収する。
                    candidates.extend(sorted(d.glob("vec0.*")))
                if candidates:
                    vec_path = str(candidates[0].resolve())
                    logger.info("sqlite-vec extension path resolved by fallback", extra={"path": vec_path})
            except Exception as exc:  # noqa: BLE001
                logger.warning("sqlite-vec extension path fallback failed", exc_info=exc)

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
            {"name": VEC_ITEMS_TABLE_NAME},
        ).fetchone()

        # 次元数の一致確認
        if existing is not None and existing[0]:
            m = re.search(r"embedding\s+float\[(\d+)\]", str(existing[0]))
            if m:
                found = int(m.group(1))
                if found != int(dimension):
                    raise RuntimeError(
                        f"{VEC_ITEMS_TABLE_NAME} embedding dimension mismatch: db={found}, expected={dimension}. "
                        "次元数を変える場合は別DBへ移行/再構築してください。"
                    )
            else:
                logger.warning("vec_items schema parse failed", extra={"sql": str(existing[0])})

        # ベクトル検索用仮想テーブルを作成
        conn.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_ITEMS_TABLE_NAME} USING vec0("
                f"item_id integer primary key, "
                f"embedding float[{dimension}] distance_metric=cosine, "
                f"kind integer partition key, "
                f"rank_day integer, "
                f"active integer"
                f")"
            )
        )
        conn.commit()


def _enable_events_fts(engine) -> None:
    """
    eventsの文字検索用にFTS5仮想テーブル（tokenize=trigram）と同期トリガーを用意する。

    - 外部コンテンツ方式（content='events'）を使用する
    - eventsのINSERT/UPDATE/DELETEに自動追従する
    """
    with engine.connect() as conn:
        # FTSテーブルの存在確認
        existed = (
            conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": EVENTS_FTS_TABLE_NAME},
            ).fetchone()
            is not None
        )

        # FTS5仮想テーブル作成
        conn.execute(
            text(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {EVENTS_FTS_TABLE_NAME} USING fts5(
                    user_text,
                    assistant_text,
                    image_summaries_json,
                    content='events',
                    content_rowid='event_id',
                    tokenize='trigram'
                )
                """
            )
        )

        # --- external content FTS はトリガーで追従させる ---
        # INSERT用
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EVENTS_FTS_TABLE_NAME}_ai
                AFTER INSERT ON events
                BEGIN
                    INSERT INTO {EVENTS_FTS_TABLE_NAME}(rowid, user_text, assistant_text, image_summaries_json)
                    VALUES (new.event_id, new.user_text, new.assistant_text, new.image_summaries_json);
                END;
                """
            )
        )
        # DELETE用
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EVENTS_FTS_TABLE_NAME}_ad
                AFTER DELETE ON events
                BEGIN
                    INSERT INTO {EVENTS_FTS_TABLE_NAME}({EVENTS_FTS_TABLE_NAME}, rowid, user_text, assistant_text, image_summaries_json)
                    VALUES ('delete', old.event_id, old.user_text, old.assistant_text, old.image_summaries_json);
                END;
                """
            )
        )
        # UPDATE用（DELETE + INSERT）
        conn.execute(
            text(
                f"""
                CREATE TRIGGER IF NOT EXISTS {EVENTS_FTS_TABLE_NAME}_au
                AFTER UPDATE ON events
                BEGIN
                    INSERT INTO {EVENTS_FTS_TABLE_NAME}({EVENTS_FTS_TABLE_NAME}, rowid, user_text, assistant_text, image_summaries_json)
                    VALUES ('delete', old.event_id, old.user_text, old.assistant_text, old.image_summaries_json);
                    INSERT INTO {EVENTS_FTS_TABLE_NAME}(rowid, user_text, assistant_text, image_summaries_json)
                    VALUES (new.event_id, new.user_text, new.assistant_text, new.image_summaries_json);
                END;
                """
            )
        )

        # 初回作成時のみ rebuild（既存の events を索引化）
        if not existed:
            conn.execute(text(f"INSERT INTO {EVENTS_FTS_TABLE_NAME}({EVENTS_FTS_TABLE_NAME}) VALUES ('rebuild')"))

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
        # --- events ---
        "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_client_created_at ON events(client_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_source_created_at ON events(source, created_at)",
        # --- event_assistant_summaries ---
        "CREATE INDEX IF NOT EXISTS idx_event_assistant_summaries_updated ON event_assistant_summaries(updated_at)",
        # --- state ---
        "CREATE INDEX IF NOT EXISTS idx_state_kind_last_confirmed ON state(kind, last_confirmed_at)",
        "CREATE INDEX IF NOT EXISTS idx_state_valid_range ON state(valid_from_ts, valid_to_ts)",
        # --- event_links / event_threads ---
        "CREATE INDEX IF NOT EXISTS idx_event_links_from ON event_links(from_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_event_links_to ON event_links(to_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_event_threads_event ON event_threads(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_event_threads_thread ON event_threads(thread_key)",
        # --- revisions ---
        "CREATE INDEX IF NOT EXISTS idx_revisions_entity ON revisions(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_revisions_created ON revisions(created_at)",
        # --- retrieval_runs ---
        "CREATE INDEX IF NOT EXISTS idx_retrieval_runs_event ON retrieval_runs(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_retrieval_runs_created ON retrieval_runs(created_at)",
        # --- event_affects ---
        "CREATE INDEX IF NOT EXISTS idx_event_affects_event ON event_affects(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_event_affects_created ON event_affects(created_at)",
        # --- event_entities / state_entities（entity索引） ---
        "CREATE INDEX IF NOT EXISTS idx_event_entities_event ON event_entities(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_event_entities_type_name ON event_entities(entity_type_norm, entity_name_norm)",
        "CREATE INDEX IF NOT EXISTS idx_state_entities_state ON state_entities(state_id)",
        "CREATE INDEX IF NOT EXISTS idx_state_entities_type_name ON state_entities(entity_type_norm, entity_name_norm)",
        # --- state_links（state↔state リンク） ---
        "CREATE INDEX IF NOT EXISTS idx_state_links_from ON state_links(from_state_id)",
        "CREATE INDEX IF NOT EXISTS idx_state_links_to ON state_links(to_state_id)",
        "CREATE INDEX IF NOT EXISTS idx_state_links_label ON state_links(label)",
        # --- jobs ---
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_run_after ON jobs(status, run_after)",
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.commit()


# --- 記憶DB: 形状チェック（マイグレーションしない） ---


def _assert_memory_db_is_new_schema(engine) -> None:
    """記憶DBが新スキーマであることを確認する。

    旧スキーマが見つかった場合は例外にして、DBファイルの削除/再作成を要求する。
    """
    with engine.connect() as conn:
        # --- 旧スキーマの痕跡を検出する ---
        old_tables = [
            "units",
            "payload_episode",
            "payload_fact",
            "payload_summary",
            "payload_loop",
            "entities",
            "unit_entities",
            "edges",
            "episode_fts",
            "vec_units",
            "unit_versions",
        ]
        rows = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ("
                + ",".join([f"'{t}'" for t in old_tables])
                + ")"
            )
        ).fetchall()
        if rows:
            found = sorted({str(r[0]) for r in rows})
            raise RuntimeError(
                "旧スキーマ（Unit/payload）の記憶DBはサポートしません。"
                f"DBファイルを削除して作り直してください。found_tables={found}"
            )

        # --- user_version で新スキーマを識別する ---
        uv = conn.execute(text("PRAGMA user_version")).fetchone()
        current = int(uv[0]) if uv and uv[0] is not None else 0
        if current not in (0, _MEMORY_DB_USER_VERSION):
            raise RuntimeError(
                f"memory DB user_version mismatch: db={current}, expected={_MEMORY_DB_USER_VERSION}. "
                "DBを削除して作り直してください。"
            )


def _set_memory_db_user_version(engine) -> None:
    """記憶DBの user_version を新スキーマに固定する。"""
    with engine.connect() as conn:
        conn.execute(text(f"PRAGMA user_version={_MEMORY_DB_USER_VERSION}"))
        conn.commit()


# --- 設定DB ---

def _verify_settings_db_user_version(engine) -> None:
    """
    設定DBの user_version を検証する。

    方針:
    - マイグレーションは扱わない（互換を切って作り直す）。
    - 旧DB（user_version=0 かつテーブルが存在する等）はエラーにする。
    """
    with engine.connect() as conn:
        uv = conn.execute(text("PRAGMA user_version")).fetchone()
        current = int(uv[0]) if uv and uv[0] is not None else 0

        # --- 未設定（0）の場合は「真っさら」だけ許容する ---
        if current == 0:
            # NOTE:
            # - 旧実装は user_version を設定していなかったため、既存DBでも 0 の可能性がある。
            # - その場合は後方互換を付けず、DBを削除して作り直してもらう。
            rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ("
                    "'global_settings','llm_presets','embedding_presets','persona_presets','addon_presets'"
                    ")"
                )
            ).fetchall()
            if rows:
                found = sorted({str(r[0]) for r in rows})
                raise RuntimeError(
                    f"settings DB user_version mismatch: db={current}, expected={_SETTINGS_DB_USER_VERSION}. "
                    f"settings.db を削除して作り直してください。found_tables={found}"
                )
            return

        if current != _SETTINGS_DB_USER_VERSION:
            raise RuntimeError(
                f"settings DB user_version mismatch: db={current}, expected={_SETTINGS_DB_USER_VERSION}. "
                "settings.db を削除して作り直してください。"
            )


def _set_settings_db_user_version(engine) -> None:
    """設定DBの user_version を新スキーマに固定する。"""
    with engine.connect() as conn:
        conn.execute(text(f"PRAGMA user_version={_SETTINGS_DB_USER_VERSION}"))
        conn.commit()


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

    # --- user_version を検証（互換は切って作り直し） ---
    _verify_settings_db_user_version(engine)

    Base.metadata.create_all(bind=engine)
    _set_settings_db_user_version(engine)
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

    # --- 旧スキーマを拒否する（運用前のためマイグレーションしない） ---
    _assert_memory_db_is_new_schema(engine)

    # --- 記憶用テーブルを作成（events/state中心） ---
    import cocoro_ghost.memory_models  # noqa: F401

    MemoryBase.metadata.create_all(bind=engine)
    _create_memory_indexes(engine)
    _enable_events_fts(engine)

    # sqlite-vec拡張を有効化
    if db_url.startswith("sqlite"):
        _enable_sqlite_vec(engine, embedding_dimension)

    # user_version を固定する（新スキーマ識別）
    _set_memory_db_user_version(engine)

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


def upsert_vec_item(
    session: Session,
    *,
    item_id: int,
    embedding: list[float],
    kind: int,
    rank_at: int,
    active: int = 1,
) -> None:
    """
    ベクトル索引を更新または挿入する（sqlite-vec仮想テーブル）。
    既存のベクトルがあれば削除してから挿入する。
    """
    embedding_json = json.dumps(embedding)
    # 日付をエポック日数に変換（検索フィルタ用）
    rank_day = int(rank_at) // 86400

    # 既存レコードを削除してから挿入
    session.execute(text(f"DELETE FROM {VEC_ITEMS_TABLE_NAME} WHERE item_id = :item_id"), {"item_id": int(item_id)})
    session.execute(
        text(
            f"""
            INSERT INTO {VEC_ITEMS_TABLE_NAME}(item_id, embedding, kind, rank_day, active)
            VALUES (:item_id, :embedding, :kind, :rank_day, :active)
            """
        ),
        {
            "item_id": int(item_id),
            "embedding": embedding_json,
            "kind": kind,
            "rank_day": rank_day,
            "active": int(active),
        },
    )


def delete_vec_item(session: Session, *, item_id: int) -> None:
    """
    ベクトル索引を削除する（sqlite-vec仮想テーブル）。

    本体テーブルの削除はFKカスケードで消えるが、vec0の仮想テーブルは
    外部キーでカスケードできないため、明示的に削除する。
    """
    session.execute(text(f"DELETE FROM {VEC_ITEMS_TABLE_NAME} WHERE item_id = :item_id"), {"item_id": int(item_id)})


def sync_vec_item_metadata(
    session: Session,
    *,
    item_id: int,
    rank_at: int,
    active: int = 1,
) -> None:
    """
    vec_itemsのメタデータカラムを同期する（埋め込みは更新しない）。
    """
    rank_day = int(rank_at) // 86400
    session.execute(
        text(
            f"""
            UPDATE {VEC_ITEMS_TABLE_NAME}
               SET rank_day = :rank_day,
                   active = :active
             WHERE item_id = :item_id
            """
        ),
        {
            "item_id": int(item_id),
            "rank_day": rank_day,
            "active": int(active),
        },
    )


def search_similar_item_ids(
    session: Session,
    *,
    query_embedding: list[float],
    k: int,
    kind: int,
    rank_day_range: tuple[int, int] | None = None,
    active_only: bool = True,
) -> list:
    """
    類似 item_id を検索する（sqlite-vec仮想テーブル）。
    コサイン距離に基づいてk件を返す。
    """
    query_json = json.dumps(query_embedding)

    # 日付範囲フィルタの構築
    where_extra = ""
    params = {
        "query": query_json,
        "k": k,
        "kind": kind,
    }
    if rank_day_range is not None:
        where_extra += " AND rank_day BETWEEN :d0 AND :d1"
        params["d0"] = int(rank_day_range[0])
        params["d1"] = int(rank_day_range[1])
    if active_only:
        where_extra += " AND active = 1"

    # ベクトル類似検索を実行
    rows = session.execute(
        text(
            f"""
            SELECT item_id, distance
            FROM {VEC_ITEMS_TABLE_NAME}
            WHERE embedding MATCH :query
              AND k = :k
              AND kind = :kind
              {where_extra}
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

    # --- Web UI: 端末跨ぎ会話の固定ID ---
    # NOTE:
    # - shared_conversation_id はサーバ側で会話の継続に使うため、必ず埋める。
    # - ここは「初期設定の作成」だけでなく「最低限の必須値の補完」も担う。
    if global_settings is not None and not getattr(global_settings, "shared_conversation_id", ""):
        import uuid

        global_settings.shared_conversation_id = str(uuid.uuid4())
        session.commit()

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
            # 記憶は本システムの中核なので、初期値は有効にする。
            memory_enabled=True,
            # 視覚（Vision）: デスクトップウォッチ（初期は無効）
            desktop_watch_enabled=False,
            desktop_watch_interval_seconds=300,
            desktop_watch_target_client_id=None,
        )
        session.add(global_settings)
        session.flush()
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
            name="sample-llm",
            archived=False,
            llm_api_key="",
            llm_model="openai/gpt-5-mini",
            max_turns_window=10,
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
            name="sample-emmbedding",
            archived=False,
            embedding_model="openai/text-embedding-3-large",
            embedding_api_key=None,
            embedding_base_url=None,
            embedding_dimension=3072,
            similar_episodes_limit=60,
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
            name="miku-sample-persona_prompt",
            archived=False,
            persona_text=prompts.get_default_persona_anchor(),
            second_person_label="マスター",
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
            name="VRM-BlendShape-Prompt",
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
