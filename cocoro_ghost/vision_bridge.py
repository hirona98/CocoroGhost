"""
視覚（Vision）ブリッジ

Ghost（サーバ）からクライアント（CocoroConsole等）へ「画像取得要求」を出し、
クライアントから返ってきた画像をチャット/デスクトップウォッチへ接続するための
インメモリ同期（request_id相関）を提供する。

注意:
- 既存の /api/events/stream を命令バスとして使う想定（publishで送る）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from cocoro_ghost import event_stream


logger = logging.getLogger(__name__)

# --- capture-response error codes（サーバー側の判断材料） ---
# NOTE:
# - クライアントが「未キャプチャを正常系としてスキップ」した場合は、images=[] と組み合わせて返す。
# - サーバーはこの文字列を見て、リトライ/抑制等の次の行動を判断する。
CAPTURE_SKIPPED_IDLE_ERROR = "capture skipped (idle)"
CAPTURE_SKIPPED_EXCLUDED_WINDOW_TITLE_ERROR = "capture skipped (excluded window title)"


@dataclass(frozen=True)
class VisionCaptureRequest:
    """クライアントへ送る画像取得要求。"""

    request_id: str
    source: str  # desktop|camera
    mode: str  # still（将来 video）
    purpose: str  # chat|desktop_watch
    timeout_ms: int


@dataclass(frozen=True)
class VisionCaptureResponse:
    """クライアントから返ってきた画像取得結果。"""

    request_id: str
    client_id: str
    images: list[str]  # data URI 形式
    client_context: Optional[Dict[str, Any]]
    error: Optional[str]


_pending_lock = threading.Lock()
_pending: dict[str, tuple[threading.Event, Optional[VisionCaptureResponse]]] = {}


def _new_request_id() -> str:
    """request_id（UUID）を生成する。"""
    return str(uuid.uuid4())


def _register_pending(request_id: str) -> threading.Event:
    """待機対象 request_id を登録し、完了通知用のEventを返す。"""
    ev = threading.Event()
    with _pending_lock:
        _pending[str(request_id)] = (ev, None)
    return ev


def _pop_response(request_id: str) -> Optional[VisionCaptureResponse]:
    """request_id に紐づく応答を取り出して破棄する（なければNone）。"""
    with _pending_lock:
        entry = _pending.pop(str(request_id), None)
    if entry is None:
        return None
    _ev, resp = entry
    return resp


def fulfill_capture_response(resp: VisionCaptureResponse) -> bool:
    """
    クライアントから届いた capture-response を、待機中の要求へ紐づける。

    Returns:
        True: 待機中の request_id に対して紐づけできた
        False: 該当 request_id が存在しない（タイムアウト済み等）
    """
    rid = str(resp.request_id or "").strip()
    if not rid:
        return False

    with _pending_lock:
        entry = _pending.get(rid)
        if entry is None:
            return False
        ev, _old = entry
        _pending[rid] = (ev, resp)
        ev.set()
        return True


def request_capture_and_wait(
    *,
    target_client_id: str,
    source: str,
    purpose: str,
    timeout_seconds: float,
    timeout_ms: int,
) -> Optional[VisionCaptureResponse]:
    """
    クライアントへ capture_request を送信し、応答を待つ。

    - client_id が未接続の場合は即座に None を返す。
    - timeout_seconds を超えたら None を返す。
    """
    cid = str(target_client_id or "").strip()
    if not cid:
        return None
    if not event_stream.is_client_connected(cid):
        logger.info("vision capture skipped (client not connected) client_id=%s purpose=%s", cid, str(purpose))
        return None

    request_id = _new_request_id()
    ev = _register_pending(request_id)

    # --- 命令を送る（バッファしない / 宛先client_id指定） ---
    req = VisionCaptureRequest(
        request_id=request_id,
        source=str(source),
        mode="still",
        purpose=str(purpose),
        timeout_ms=int(timeout_ms),
    )
    event_stream.publish(
        type="vision.capture_request",
        event_id=0,
        data={
            "request_id": req.request_id,
            "source": req.source,
            "mode": req.mode,
            "purpose": req.purpose,
            "timeout_ms": req.timeout_ms,
        },
        target_client_id=cid,
    )
    logger.info(
        "vision capture request sent request_id=%s client_id=%s source=%s purpose=%s timeout_ms=%s",
        request_id,
        cid,
        str(source),
        str(purpose),
        int(timeout_ms),
    )

    # --- 応答を待つ ---
    ok = ev.wait(timeout=float(timeout_seconds))
    if not ok:
        # タイムアウト時は待機エントリを破棄
        _pop_response(request_id)
        logger.info(
            "vision capture timed out request_id=%s client_id=%s source=%s purpose=%s",
            request_id,
            cid,
            str(source),
            str(purpose),
        )
        return None
    return _pop_response(request_id)


def is_capture_skipped_error(error: str | None) -> bool:
    """capture-response の error が「スキップ（正常系）」かを判定する。"""

    s = str(error or "").strip()
    if not s:
        return False
    return s in {
        CAPTURE_SKIPPED_IDLE_ERROR,
        CAPTURE_SKIPPED_EXCLUDED_WINDOW_TITLE_ERROR,
    }


def is_capture_skipped_idle(error: str | None) -> bool:
    """capture-response の error が「アイドルによるスキップ」かを判定する。"""

    return str(error or "").strip() == CAPTURE_SKIPPED_IDLE_ERROR


def is_capture_skipped_excluded_window_title(error: str | None) -> bool:
    """capture-response の error が「除外タイトル一致によるスキップ」かを判定する。"""

    return str(error or "").strip() == CAPTURE_SKIPPED_EXCLUDED_WINDOW_TITLE_ERROR
