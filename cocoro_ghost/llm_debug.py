"""
LLM送受信のデバッグ出力ユーティリティ

LLMとの通信内容をデバッグ用に整形・出力する。
LLMクライアント実装から独立しており、任意の箇所に差し込み可能。

主な機能:
- JSONっぽい文字列の補正とパース（フェンス除去、末尾カンマ修正等）
- 秘匿情報（api_key、token等）のマスク

使い方例:
    from cocoro_ghost.llm_debug import log_llm_payload
    log_llm_payload(logger, "LLM request", payload_dict)
    log_llm_payload(logger, "LLM response", response_text)
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable


# ここにあるキーは、入出力のデバッグ時に値をマスクする。
# NOTE: 完全ではないが、誤ってログに出してしまう事故を減らす。
_DEFAULT_REDACT_KEYS = {
    "openai_api_key",
    "anthropic_api_key",
    "token",
    "access_token",
    "refresh_token",
    "x-api-key",
}

# ログに出さないキー（実送信/受信でも表示しない方針）
_DEFAULT_DROP_KEYS = {
    "api_key",
    "authorization",
    "model",
    "max_tokens",
    "response_format",
    "stream",
    "temperature",
}


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


def _try_parse_json_text(text: str) -> tuple[bool, Any]:
    """
    文字列がJSON単体ならパースする。

    JSON以外のテキストはそのまま扱う。
    """
    # 空文字は対象外
    if not text:
        return False, text

    # コードフェンスを除去して判定する
    stripped = _strip_code_fences(text).strip()
    if not stripped:
        return False, text

    # 先頭/末尾がJSONっぽくない場合は除外する
    starts_obj = stripped.startswith("{") and stripped.endswith("}")
    starts_arr = stripped.startswith("[") and stripped.endswith("]")
    if not (starts_obj or starts_arr):
        return False, text

    # 先頭のJSON値を取り出し、全文一致ならJSONとみなす
    candidate = _extract_first_json_value(stripped)
    if not candidate:
        return False, text
    if candidate.strip() != stripped:
        return False, text

    # JSONとしてパースする（必要なら最低限の修復を試す）
    try:
        return True, json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _repair_json_like_text(candidate)
        try:
            return True, json.loads(repaired)
        except json.JSONDecodeError:
            return False, text


def _parse_embedded_json_strings(obj: Any, *, max_depth: int = 6) -> Any:
    """
    dict/list内のJSON文字列を再帰的にパースする。

    ログ表示用に、人間が読める構造へ整形する目的。
    """
    # 深さ制限で無限再帰を回避する
    if max_depth <= 0:
        return obj

    # dictは各値を再帰的に処理する
    if isinstance(obj, dict):
        parsed: dict[str, Any] = {}
        for k, v in obj.items():
            parsed[str(k)] = _parse_embedded_json_strings(v, max_depth=max_depth - 1)
        return parsed

    # listは各要素を再帰的に処理する
    if isinstance(obj, list):
        return [_parse_embedded_json_strings(v, max_depth=max_depth - 1) for v in obj]

    # tupleはtupleのまま再帰処理する
    if isinstance(obj, tuple):
        return tuple(_parse_embedded_json_strings(v, max_depth=max_depth - 1) for v in obj)

    # 文字列はJSONとして解釈できるなら構造化する
    if isinstance(obj, str):
        parsed, value = _try_parse_json_text(obj)
        if parsed:
            return _parse_embedded_json_strings(value, max_depth=max_depth - 1)
        return obj

    # それ以外はそのまま返す
    return obj


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
    drop_keys: Iterable[str] | None = None,
    placeholder: str = "***",
    max_depth: int = 12,
) -> Any:
    """dict/listを再帰的に走査して、秘匿情報っぽい値をマスクする。"""

    keys = {k.lower() for k in (redact_keys or _DEFAULT_REDACT_KEYS)}
    drop = {k.lower() for k in (drop_keys or _DEFAULT_DROP_KEYS)}

    def _walk(v: Any, depth: int) -> Any:
        if depth <= 0:
            return "..."  # 無限再帰対策（デバッグ用途なので簡易に丸める）

        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, vv in v.items():
                lk = str(k).lower()
                # 指定キーはログから完全に除外する
                if lk in drop:
                    continue
                if lk in keys:
                    out[str(k)] = placeholder
                else:
                    out[str(k)] = _walk(vv, depth - 1)
            return out

        if isinstance(v, list):
            return [_walk(x, depth - 1) for x in v]

        if isinstance(v, tuple):
            return tuple(_walk(x, depth - 1) for x in v)

        # 文字列のAuthorization: Bearer ... 等はログから除外する
        if isinstance(v, str):
            s = v
            if re.search(r"\bBearer\s+\S+", s, re.IGNORECASE):
                return "(authorization omitted)"
            return s

        return v

    return _walk(_to_serializable(obj), max_depth)


def format_debug_payload(
    payload: Any,
    *,
    max_chars: int = 8000,
    max_value_chars: int = 0,
    try_parse_json_string: bool = True,
) -> str:
    """payloadをデバッグ向けに文字列化する（JSONなら見やすく整形）。"""
    # まずシリアライズ可能な形に変換する
    serializable = _to_serializable(payload)
    # 文字列中のJSONを構造化して読みやすくする
    serializable = _parse_embedded_json_strings(serializable)
    # 秘匿情報をマスクする
    serializable = redact_secrets(serializable)
    # Value長を制限してログの肥大化を抑える
    serializable = limit_json_value_lengths(serializable, max_value_chars=max_value_chars)

    # dict/list ならそのまま pretty JSON
    if isinstance(serializable, (dict, list)):
        try:
            s = json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True)
            return truncate_for_log(s, max_chars)
        except Exception:
            # フォールバック
            return truncate_for_log(str(serializable), max_chars)

    # 文字列（JSONっぽいなら抽出→補正→パース→pretty）
    if isinstance(serializable, str) and try_parse_json_string:
        candidate = _extract_first_json_value(serializable)
        if candidate:
            try:
                parsed = json.loads(candidate)
                masked = redact_secrets(parsed)
                masked = limit_json_value_lengths(masked, max_value_chars=max_value_chars)
                return truncate_for_log(json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True), max_chars)
            except Exception:
                repaired = _repair_json_like_text(candidate)
                try:
                    parsed = json.loads(repaired)
                    masked = redact_secrets(parsed)
                    masked = limit_json_value_lengths(masked, max_value_chars=max_value_chars)
                    return truncate_for_log(json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True), max_chars)
                except Exception:
                    pass
        return truncate_for_log(serializable, max_chars)

    return truncate_for_log(str(serializable), max_chars)


def truncate_for_log(text: str, limit: int) -> str:
    """ログ出力用にテキストを切り詰める。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(Cut)"


def limit_json_value_lengths(obj: Any, *, max_value_chars: int, max_depth: int = 12) -> Any:
    """JSON相当のオブジェクトで、各Value（文字列）の最大長を制限する。"""
    if max_value_chars <= 0:
        return obj

    def _walk(v: Any, depth: int) -> Any:
        if depth <= 0:
            return "..."

        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, vv in v.items():
                out[str(k)] = _walk(vv, depth - 1)
            return out

        if isinstance(v, list):
            return [_walk(x, depth - 1) for x in v]

        if isinstance(v, tuple):
            return tuple(_walk(x, depth - 1) for x in v)

        if isinstance(v, str):
            # 先頭/末尾を残し、中間を省略する。
            if len(v) <= max_value_chars * 2:
                return v
            head = v[:max_value_chars]
            tail = v[-max_value_chars:]
            return head + f"...(Cut, {len(v)})..." + tail

        return v

    return _walk(obj, max_depth)


def normalize_llm_log_level(llm_log_level: str | None) -> str:
    """LLM送受信ログレベルを正規化する。"""
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
    max_value_chars: int = 0,
) -> None:
    """LLMの送受信payloadをログ出力する。

    - DEBUG: 内容を整形して出力
    - INFO/OFF: 内容は出さない

    loggerは標準loggingのLogger互換（debug/info等）を想定。
    """

    if logger is None:
        return

    level = normalize_llm_log_level(llm_log_level)
    if level != "DEBUG":
        return

    text = format_debug_payload(payload, max_chars=max_chars, max_value_chars=max_value_chars)
    try:
        logger.debug("%s: %s", label, text)
    except Exception:
        # 最後の砦
        print(f"{label}: {text}")
