"""
Web UI 用の Cookie セッション管理（メモリ保存）。

目的:
    - ブラウザからの API 呼び出し（/api/chat, /api/events/stream）を Cookie セッションで認証する。
    - 単一ユーザー前提のため、ユーザーIDなどは扱わず「有効なセッションIDかどうか」だけを持つ。

方針:
    - セッションはメモリ辞書に保存する（プロセス再起動でログアウト扱い）。
    - セッションの有効期限は 24 時間（アクセスがあれば延長＝スライディング）。
    - Cookie 側の Max-Age は長めでもよい（サーバ側が無効化するため）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionInfo:
    """セッション情報（最小）。"""

    session_id: str
    expires_at_unix: float


class WebSessionStore:
    """Cookie セッションをメモリで管理する。"""

    def __init__(self, *, ttl_seconds: int = 24 * 60 * 60) -> None:
        # --- セッション寿命（アイドルタイムアウト） ---
        self._ttl_seconds = int(ttl_seconds)

        # --- session_id -> expires_at_unix ---
        self._sessions: dict[str, float] = {}

        # --- 単純な排他 ---
        self._lock = Lock()

    def create(self) -> SessionInfo:
        """新しいセッションを作成して返す。"""

        # --- 一意なIDを作る ---
        sid = str(uuid4())
        expires = time.time() + float(self._ttl_seconds)

        # --- 保存 ---
        with self._lock:
            self._sessions[sid] = expires

        return SessionInfo(session_id=sid, expires_at_unix=expires)

    def delete(self, session_id: str) -> None:
        """セッションを削除する（存在しなくてもよい）。"""

        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)

    def validate_and_touch(self, session_id: str) -> bool:
        """
        セッションが有効なら期限を延長して True を返す。
        無効なら False を返す。
        """

        sid = str(session_id or "").strip()
        if not sid:
            return False

        now = time.time()
        with self._lock:
            expires = self._sessions.get(sid)
            if expires is None:
                return False
            if float(expires) <= now:
                # --- 期限切れは削除して無効 ---
                self._sessions.pop(sid, None)
                return False

            # --- スライディング延長 ---
            new_expires = now + float(self._ttl_seconds)
            self._sessions[sid] = new_expires

        return True

    def cleanup_expired(self) -> int:
        """期限切れセッションを掃除して削除件数を返す。"""

        now = time.time()
        removed = 0
        with self._lock:
            for sid, expires in list(self._sessions.items()):
                if float(expires) <= now:
                    self._sessions.pop(sid, None)
                    removed += 1
        if removed:
            logger.info("web session expired cleanup", extra={"removed": removed})
        return removed


_global_session_store: WebSessionStore | None = None


def get_web_session_store() -> WebSessionStore:
    """グローバルな WebSessionStore を返す（シングルトン）。"""

    global _global_session_store
    if _global_session_store is None:
        _global_session_store = WebSessionStore()
    return _global_session_store

