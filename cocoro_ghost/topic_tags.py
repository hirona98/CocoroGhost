"""
topic_tags 正規化ユーティリティ

トピックタグの正規化・重複排除・JSON変換を行う。
タグはNFKC正規化され、ソートされた一意のリストとして管理される。
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Iterable, List


def canonicalize_topic_tags(tags: Iterable[Any]) -> List[str]:
    """
    タグ文字列を正規化する。

    NFKC正規化・空白除去を行い、重複を排除してソートしたリストを返す。
    """
    normalized: list[str] = []
    for tag in tags:
        s = "" if tag is None else str(tag)
        s = unicodedata.normalize("NFKC", s).strip()
        if not s:
            continue
        normalized.append(s)
    return sorted(set(normalized))


def dumps_topic_tags_json(tags: Iterable[Any]) -> str:
    """
    タグを正規化してJSON文字列にダンプする。

    canonicalize_topic_tagsで正規化後、JSON配列として出力する。
    """
    return json.dumps(canonicalize_topic_tags(tags), ensure_ascii=False, separators=(",", ":"))


def canonicalize_topic_tags_json(text: str) -> str:
    """
    JSON配列文字列を正規化する。

    入力JSONをパースし、タグを正規化して再度JSON文字列として返す。
    """
    loaded = json.loads(text)
    if not isinstance(loaded, list):
        raise ValueError("topic_tags must be a JSON array")
    return dumps_topic_tags_json(loaded)
