"""
LLM送受信のデバッグ出力ユーティリティ

LLMとの通信内容をデバッグ用に整形・出力する。
LLMクライアント実装から独立しており、任意の箇所に差し込み可能。

主な機能:
- JSONっぽい文字列の補正とパース（フェンス除去、末尾カンマ修正等）
- 秘匿情報（api_key、token等）のマスク
- 環境変数 COCORO_LLM_IO_DEBUG=1 で強制出力

使い方例:
    from cocoro_ghost.llm_debug import log_llm_payload
    log_llm_payload(logger, "LLM request", payload_dict)
    log_llm_payload(logger, "LLM response", response_text)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable


# ここにあるキーは、入出力のデバッグ時に値をマスクする。
# NOTE: 完全ではないが、誤ってログに出してしまう事故を減らす。
_DEFAULT_REDACT_KEYS = {
    "api_key",
    "openai_api_key",
    "anthropic_api_key",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "x-api-key",
}


def _truthy_env(name: str) -> bool:
    """環境変数の真偽値っぽい値を解釈する。"""
    v = (os.getenv(name) or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _truncate_for_log(text: str, limit: int) -> str:
    """ログ向けに文字数を制限する。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """```json ... ``` のようなフェンスがあれば中身だけを取り出す。"""
    if not text:
        return ""
    m = _JSON_FENCE_RE.search(text)
    return m.group(1) if m else text


def _extract_first_json_value(text: str) -> str:
    """文字列から最初の JSON 値（object/array）らしき部分を抜き出す。

    LLMの出力には前後に説明文が付くことがあるため、最初の '{' / '[' を起点に
    文字列リテラルを考慮しつつ括弧を数えて切り出す。
    """

    text = _strip_code_fences(text).strip()
    if not text:
        return ""

    obj_i = text.find("{")
    arr_i = text.find("[")
    if obj_i == -1 and arr_i == -1:
        return text

    if obj_i == -1:
        start = arr_i
        open_ch, close_ch = "[", "]"
    elif arr_i == -1:
        start = obj_i
        open_ch, close_ch = "{", "}"
    else:
        start = obj_i if obj_i < arr_i else arr_i
        open_ch, close_ch = ("{", "}") if start == obj_i else ("[", "]")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == open_ch:
            depth += 1
            continue
        if ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    # 閉じ括弧が見つからなかった場合は残りを返す（デバッグなので保守的に）。
    return text[start:]


def _escape_control_chars_in_json_strings(text: str) -> str:
    """JSON文字列内に混入した生の改行/タブ等をエスケープする。"""
    if not text:
        return ""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue

        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _repair_json_like_text(text: str) -> str:
    """LLMが生成しがちな「ほぼJSON」を、最低限パースできるように整形する。"""
    if not text:
        return ""
    s = text.strip()
    # スマートクォート対策
    s = s.replace("“", '"').replace("”", '"')
    # 文字列内の改行/タブ等
    s = _escape_control_chars_in_json_strings(s)
    # 末尾カンマ
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _to_serializable(obj: Any) -> Any:
    """json.dumpsできる形に寄せる（失敗しても落とさない）。"""
    if obj is None:
        return None

    # pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass

    # pydantic v1
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass

    if is_dataclass(obj):
        try:
            return asdict(obj)
        except Exception:
            pass

    if isinstance(obj, (dict, list, tuple)):
        return obj

    # bytes は UTF-8 で頑張って復元
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8")
        except Exception:
            return obj.decode("utf-8", errors="replace")

    return str(obj)


def redact_secrets(
    obj: Any,
    *,
    redact_keys: Iterable[str] | None = None,
    placeholder: str = "***",
    max_depth: int = 12,
) -> Any:
    """dict/listを再帰的に走査して、秘匿情報っぽい値をマスクする。"""

    keys = {k.lower() for k in (redact_keys or _DEFAULT_REDACT_KEYS)}

    def _walk(v: Any, depth: int) -> Any:
        if depth <= 0:
            return "..."  # 無限再帰対策（デバッグ用途なので簡易に丸める）

        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, vv in v.items():
                lk = str(k).lower()
                if lk in keys:
                    out[str(k)] = placeholder
                else:
                    out[str(k)] = _walk(vv, depth - 1)
            return out

        if isinstance(v, list):
            return [_walk(x, depth - 1) for x in v]

        if isinstance(v, tuple):
            return tuple(_walk(x, depth - 1) for x in v)

        # 文字列のAuthorization: Bearer ... 等は丸ごとマスク
        if isinstance(v, str):
            s = v
            if re.search(r"\bBearer\s+\S+", s, re.IGNORECASE):
                return re.sub(r"\bBearer\s+\S+", "Bearer ***", s, flags=re.IGNORECASE)
            return s

        return v

    return _walk(_to_serializable(obj), max_depth)


def format_debug_payload(
    payload: Any,
    *,
    max_chars: int = 8000,
    try_parse_json_string: bool = True,
) -> str:
    """payloadをデバッグ向けに文字列化する（JSONなら見やすく整形）。"""

    serializable = redact_secrets(payload)

    # dict/list ならそのまま pretty JSON
    if isinstance(serializable, (dict, list)):
        try:
            s = json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True)
            return _truncate_for_log(s, max_chars)
        except Exception:
            # フォールバック
            return _truncate_for_log(str(serializable), max_chars)

    # 文字列（JSONっぽいなら抽出→補正→パース→pretty）
    if isinstance(serializable, str) and try_parse_json_string:
        candidate = _extract_first_json_value(serializable)
        if candidate:
            try:
                parsed = json.loads(candidate)
                masked = redact_secrets(parsed)
                return _truncate_for_log(json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True), max_chars)
            except Exception:
                repaired = _repair_json_like_text(candidate)
                try:
                    parsed = json.loads(repaired)
                    masked = redact_secrets(parsed)
                    return _truncate_for_log(json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True), max_chars)
                except Exception:
                    pass
        return _truncate_for_log(serializable, max_chars)

    return _truncate_for_log(str(serializable), max_chars)


def normalize_llm_log_level(llm_log_level: str | None) -> str:
    """LLM送受信ログレベルを正規化する。"""
    if _truthy_env("COCORO_LLM_IO_DEBUG"):
        return "DEBUG"
    level = (llm_log_level or "INFO").upper()
    if level not in {"DEBUG", "INFO", "OFF"}:
        return "INFO"
    return level


def log_llm_payload(
    logger: Any,
    label: str,
    payload: Any,
    *,
    llm_log_level: str = "INFO",
    max_chars: int = 8000,
) -> None:
    """LLMの送受信payloadをログ出力する。

    - DEBUG: 内容を整形して出力
    - INFO/OFF: 内容は出さない
    - COCORO_LLM_IO_DEBUG=1 なら強制的にDEBUGで出力

    loggerは標準loggingのLogger互換（debug/info等）を想定。
    """

    if logger is None:
        return

    level = normalize_llm_log_level(llm_log_level)
    if level != "DEBUG":
        return

    text = format_debug_payload(payload, max_chars=max_chars)
    try:
        logger.info("%s: %s", label, text)
    except Exception:
        # 最後の砦
        print(f"{label}: {text}")
