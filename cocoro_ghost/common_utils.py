"""
共通ユーティリティ（JSON/テキスト/LLM出力の最小セット）。

目的:
    - `worker.py` と `memory.py` に重複していた小物関数を集約する。
    - 仕様（JSONの安定ダンプ、[face:*]除去、LLMのJSON抽出）を1箇所で管理する。

方針:
    - 「何でも入れる utils」にはしない。重複が多く、仕様が揺れる領域だけを扱う。
    - 例外を投げる/投げないは用途が違うため、APIを分ける（or_raise / or_none）。
"""

from __future__ import annotations

import json
import re
from typing import Any


def json_dumps(payload: Any) -> str:
    """DB保存向けにJSONを安定した形式でダンプする（日本語保持）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def json_loads_maybe(text_in: str) -> dict[str, Any]:
    """JSON文字列をdictとして読む（失敗時は空dict）。"""
    try:
        obj = json.loads(str(text_in or ""))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def parse_json_str_list(text_in: str | None) -> list[str]:
    """JSON文字列を list[str] として読む（失敗時は空list）。"""
    s = str(text_in or "").strip()
    if not s:
        return []
    try:
        obj = json.loads(s)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(obj, list):
        return []
    out: list[str] = []
    for item in obj:
        t = str(item or "").replace("\n", " ").replace("\r", " ").strip()
        if not t:
            continue
        out.append(t)
    return out


def strip_face_tags(text_in: str) -> str:
    """
    テキストから会話装飾タグ（例: [face:Joy]）を除去する。

    NOTE:
        - [face:*] はユーザーに見せる返答用の表記であり、内部の記憶（WritePlan/state/event_affect）には不要。
        - LLMが混入させてもDBへ保存しないように、保存前に必ず除去する。
    """
    s = str(text_in or "")
    if not s:
        return ""

    # --- [face:...] 形式を一律で消す（種類は固定しない） ---
    s2 = re.sub(r"\[face:[^\]]+\]", "", s)

    # --- 余計な空白を整える ---
    return " ".join(s2.replace("\r", " ").replace("\n", " ").split()).strip()


def first_choice_content(resp: Any) -> str:
    """LiteLLMのレスポンスから最初のchoiceのcontentを取り出す。"""
    try:
        return str(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        try:
            return str(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            return ""


def parse_first_json_object_or_none(text: str) -> dict[str, Any] | None:
    """LLM出力から最初のJSONオブジェクトを抽出してdictとして返す（失敗時はNone）。"""
    s = str(text or "").strip()
    if not s:
        return None

    # --- llm_client の内部ユーティリティで抽出/修復する ---
    from cocoro_ghost.llm_client import _extract_first_json_value, _repair_json_like_text  # noqa: PLC0415

    candidate = _extract_first_json_value(s)
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            obj = json.loads(_repair_json_like_text(candidate))
        except Exception:  # noqa: BLE001
            return None
    return obj if isinstance(obj, dict) else None


def parse_first_json_object_or_raise(text_in: str) -> dict[str, Any]:
    """LLM出力から最初のJSONオブジェクトを抽出してdictとして返す（失敗時は例外）。"""
    obj = parse_first_json_object_or_none(text_in)
    if obj is None:
        raise RuntimeError("JSON extract failed")
    return obj

