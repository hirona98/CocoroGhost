"""topic_tags 正規化ユーティリティ。"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Iterable, List


def canonicalize_topic_tags(tags: Iterable[Any]) -> List[str]:
    """タグ文字列を正規化（NFKC/strip）し、重複排除してソートしたlistを返す。"""
    normalized: list[str] = []
    for tag in tags:
        s = "" if tag is None else str(tag)
        s = unicodedata.normalize("NFKC", s).strip()
        if not s:
            continue
        normalized.append(s)
    return sorted(set(normalized))


def dumps_topic_tags_json(tags: Iterable[Any]) -> str:
    """タグを正規化してJSON配列文字列にダンプする。"""
    return json.dumps(canonicalize_topic_tags(tags), ensure_ascii=False, separators=(",", ":"))


def canonicalize_topic_tags_json(text: str) -> str:
    """JSON配列文字列（topic_tags）を正規化してJSON文字列として返す。"""
    loaded = json.loads(text)
    if not isinstance(loaded, list):
        raise ValueError("topic_tags must be a JSON array")
    return dumps_topic_tags_json(loaded)
