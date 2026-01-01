"""
Factポリシー（predicate語彙・正規化・競合解決の共通定義）

このプロジェクトのFactは「人間らしい会話に必要な内部メモ（安定知識）」であり、
ドメイン固有の語彙を無制限に増やすのではなく、少数の“認知プリミティブ”へ寄せる。

目的:
- predicateの語彙爆発を防ぐ（同義語・近義語の増殖を止める）
- “変わり得る事実”は履歴を保持しつつ、現在状態を注入で安定させる
- Worker/MemoryPack/Prompt の三箇所で同じルールを共有する
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


# ---
# Fact predicate（制御語彙）
# ---

# 述語は「どんな対象にも適用できるプリミティブ」に限定する。
# NOTE:
# - ドメイン名詞（romantic_partner_is / primary_vehicle_is 等）をpredicate化しない。
# - “関係性”は entity relation（Edge）側で表現する前提。
CANONICAL_FACT_PREDICATES: frozenset[str] = frozenset(
    {
        # Identity / 呼称
        "name_is",
        "is_addressed_as",
        # Preference / tendency（set）
        "likes",
        "dislikes",
        "prefers",
        "avoids",
        "values",
        "interested_in",
        "habit",
        # Resources / tools（set）
        "uses",
        "owns",
        # Role / affiliation / location（set寄り。必要なら注入側でcap）
        "role_is",
        "affiliated_with",
        "located_in",
        # Settings（exclusive寄り）
        "operates_on",
        "timezone_is",
        "locale_is",
        "preferred_language_is",
        "preferred_input_style_is",
        # Goals / constraints（exclusive寄り）
        "goal_is",
        "constraint_is",
        # Event
        "first_met_at",
    }
)


# LLM出力の揺れを吸収する最低限のalias。
# ここは“確実に同義”なものだけに絞る（無理に寄せると誤マージの原因になる）。
PREDICATE_ALIASES: dict[str, str] = {
    # 名前系 → name_is
    "identity": "name_is",
    "has_name": "name_is",
    "is_named": "name_is",
    # 呼称系 → is_addressed_as
    "is_called": "is_addressed_as",
    "called_by": "is_addressed_as",
    "is_called_by": "is_addressed_as",
    "is_addressed_as": "is_addressed_as",
    # uses系
    "uses_application": "uses",
    "uses_tool": "uses",
    "uses_tech": "uses",
}


# “現在状態として1つに畳みたい”predicate（subject+predicateで最新を採用）。
EXCLUSIVE_PREDICATES: frozenset[str] = frozenset(
    {
        "name_is",
        "operates_on",
        "timezone_is",
        "locale_is",
        "preferred_language_is",
        "preferred_input_style_is",
        "goal_is",
        "constraint_is",
        # NOTE: role/affiliation/location は状況により複数あり得るため set 側に置く。
    }
)


# “履歴として意味がある”predicate（基本は最初だけ残す）。
EVENT_PREDICATES: frozenset[str] = frozenset({"first_met_at"})


_WHITESPACE_RE = re.compile(r"\s+")


def canonicalize_fact_predicate(raw: str) -> Optional[str]:
    """
    Fact predicate を正規化し、制御語彙に収まるものだけ返す。

    - alias を正規形へ寄せる
    - 制御語彙に無いものは None（=採用しない）
    """
    p = str(raw or "").strip()
    if not p:
        return None
    p = PREDICATE_ALIASES.get(p, p)
    return p if p in CANONICAL_FACT_PREDICATES else None


def is_exclusive_predicate(predicate: str) -> bool:
    """predicate が exclusive（現在1つ）として扱うべきか判定する。"""
    return str(predicate or "") in EXCLUSIVE_PREDICATES


def is_event_predicate(predicate: str) -> bool:
    """predicate が event（履歴）として扱うべきか判定する。"""
    return str(predicate or "") in EVENT_PREDICATES


def normalize_object_text_for_key(text: Optional[str]) -> Optional[str]:
    """
    object_text の比較キー用の最小正規化。

    日本語の表記ゆれ（かな/漢字など）を無理に正規化すると誤マージしやすい。
    ここでは「空白ノイズ」を潰す程度に留める。
    """
    if text is None:
        return None
    s = str(text)
    s = s.strip()
    if not s:
        return None
    # 空白の揺れ（全角/半角含む）だけ縮約する。
    s = _WHITESPACE_RE.sub(" ", s)
    return s


def fact_identity_key(
    *,
    subject_entity_id: Optional[int],
    predicate: str,
    object_entity_id: Optional[int],
    object_text: Optional[str],
) -> Tuple[Optional[int], str, Optional[int], Optional[str]]:
    """
    Factの同一性キー（同一三つ組）を返す。

    - object_entity_id がある場合は entity を正とし、object_text は補助扱い
    - object_entity_id が無い場合は object_text の最小正規化で比較する
    """
    p = str(predicate or "")
    if object_entity_id is not None:
        return (subject_entity_id, p, int(object_entity_id), None)
    return (subject_entity_id, p, None, normalize_object_text_for_key(object_text))


def effective_fact_ts(
    *,
    occurred_at: Optional[int],
    created_at: Optional[int],
    valid_from: Optional[int],
) -> int:
    """
    Factの“有効開始”の基準時刻を返す。

    優先順位:
    1) valid_from（明示的な有効開始）
    2) occurred_at（元エピソードの出来事時刻）
    3) created_at（抽出/保存の時刻）
    4) それでも無ければ 0
    """
    if isinstance(valid_from, int) and valid_from > 0:
        return int(valid_from)
    if isinstance(occurred_at, int) and occurred_at > 0:
        return int(occurred_at)
    if isinstance(created_at, int) and created_at > 0:
        return int(created_at)
    return 0

