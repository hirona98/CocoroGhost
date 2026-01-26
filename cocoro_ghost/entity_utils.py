"""
エンティティ（person/org/place/project/tool）の正規化ユーティリティ。

目的:
    - WritePlan が返す `event_annotations.entities` は LLM 出力なので、表記ゆれやノイズが混ざり得る。
    - 検索用の索引（event_entities/state_entities）では、正規化したキーで扱う必要がある。

方針:
    - シンプルな正規化（Unicode NFKC + 空白整形 + 小文字化）で「同一性」を安定させる。
    - 強い推測や外部辞書は使わない（運用前・依存を増やさない）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


# --- entity type 正規化 ---
#
# NOTE:
# - このリポジトリでは entity type を 5種に固定する:
#   person / org / place / project / tool
# - LLM が大文字や別名を返しても、ここで吸収して索引へ落とす。
_TYPE_ALIASES: dict[str, str] = {
    # person
    "person": "person",
    "people": "person",
    "human": "person",
    "individual": "person",
    "per": "person",
    "p": "person",
    "人物": "person",
    "人": "person",
    "ヒト": "person",
    "PERSON": "person",
    # org
    "org": "org",
    "organization": "org",
    "organisation": "org",
    "company": "org",
    "team": "org",
    "group": "org",
    "チーム": "org",
    "班": "org",
    "部": "org",
    "団体": "org",
    "組織": "org",
    "会社": "org",
    "ORG": "org",
    # place
    "place": "place",
    "location": "place",
    "loc": "place",
    "city": "place",
    "country": "place",
    "場所": "place",
    "地名": "place",
    "国": "place",
    "PLACE": "place",
    # project
    "project": "project",
    "proj": "project",
    "initiative": "project",
    "プロジェクト": "project",
    "作品": "project",
    "企画": "project",
    "PROJECT": "project",
    # tool
    "tool": "tool",
    "app": "tool",
    "software": "tool",
    "library": "tool",
    "framework": "tool",
    "ツール": "tool",
    "TOOL": "tool",
}


_SPACE_RE = re.compile(r"\s+")


def clamp_01(x: Any) -> float:
    """0.0..1.0 にクランプする（不正値は0.0扱い）。"""
    try:
        v = float(x)
    except Exception:  # noqa: BLE001
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(v)))


def normalize_entity_type(raw_type: Any) -> str | None:
    """
    entity type を正規化して `person/org/place/project/tool` のいずれかへ寄せる。

    Args:
        raw_type: LLM出力の type（例: "PERSON", "org", "場所" など）。

    Returns:
        正規化type（"person" 等）か、解釈できない場合は None。
    """

    # --- 入力を正規化 ---
    s = str(raw_type or "").strip()
    if not s:
        return None

    # --- まずはそのまま引く ---
    if s in _TYPE_ALIASES:
        return str(_TYPE_ALIASES[s])

    # --- 大文字小文字などを吸収 ---
    s2 = unicodedata.normalize("NFKC", s).strip()
    s2_l = s2.lower()
    if s2 in _TYPE_ALIASES:
        return str(_TYPE_ALIASES[s2])
    if s2_l in _TYPE_ALIASES:
        return str(_TYPE_ALIASES[s2_l])

    # --- 旧表現の一部だけは吸収（運用前でも、LLM揺れは現実的に起きる） ---
    # NOTE:
    # - THING/TOPIC は本プロジェクトの「正規type」ではない。
    # - ただし、雑に落とすよりも project/tool へ寄せた方が索引の実用性が高いケースがある。
    s3 = s2_l.replace("-", "_")
    if s3 in ("topic", "concept"):
        return "project"
    if s3 in ("thing", "product"):
        return "tool"

    return None


def normalize_entity_name(raw_name: Any, *, max_len: int = 80) -> str:
    """
    entity name を索引用に正規化する。

    方針:
        - Unicode NFKCで表記ゆれを吸収（全角/半角など）
        - 連続空白を1つに圧縮
        - 小文字化（英字の揺れを吸収。日本語への影響は小さい）

    Args:
        raw_name: LLM出力の name。
        max_len: 過度に長い名前を切り詰める上限。
    """

    # --- 正規化 ---
    s = unicodedata.normalize("NFKC", str(raw_name or "")).strip()
    if not s:
        return ""
    s = _SPACE_RE.sub(" ", s)
    s = s.casefold()
    s = s.strip()

    # --- 文字数上限（DB/索引の健全性を守る） ---
    limit = max(1, int(max_len))
    if len(s) > limit:
        s = s[:limit].strip()
    return s


# --- よくあるノイズ語（正規化済み） ---
# NOTE: entity name は normalize_entity_name() を通して比較する。
_STOP_NAMES_RAW = [
    "私",
    "わたし",
    "僕",
    "ぼく",
    "俺",
    "おれ",
    "自分",
    "あなた",
    "きみ",
    "君",
    "マスター",
    "master",
]
_STOP_NAMES_NORM = {normalize_entity_name(x) for x in _STOP_NAMES_RAW}


def _is_noise_entity_name(name_norm: str) -> bool:
    """索引に入れる価値が薄い名前を弾く（ノイズ削減）。"""

    # --- 空は当然除外 ---
    s = str(name_norm or "").strip()
    if not s:
        return True

    # --- よくある一人称/二人称 ---
    # NOTE: ここは「会話の常連語」を落として索引を健全化する意図。
    if s in _STOP_NAMES_NORM:
        return True

    # --- 記号だけ/短すぎるもの ---
    if len(s) <= 1:
        return True
    if all((not ch.isalnum()) for ch in s):
        return True

    return False


def normalize_entities(entities_raw: Any) -> list[dict[str, Any]]:
    """
    WritePlan の `event_annotations.entities` を正規化して返す。

    Returns:
        正規化済みの list[{"type": "...", "name": "...", "confidence": 0.0}]。
        - type は person/org/place/project/tool のいずれか
        - name はトリム済み（表示/監査用）
        - confidence は 0.0..1.0
    """

    # --- 入力型を正規化 ---
    entities = entities_raw if isinstance(entities_raw, list) else []

    # --- 一旦、正規化キーで集約して重複を潰す ---
    # NOTE: LLMが同じ名前を複数回返した場合、最大confidenceを採用する。
    best_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entities:
        if not isinstance(item, dict):
            continue

        # --- type/name/confidence を読む ---
        t_norm = normalize_entity_type(item.get("type"))
        if t_norm is None:
            continue
        name_raw = str(item.get("name") or "").strip()
        if not name_raw:
            continue
        name_norm = normalize_entity_name(name_raw)
        if _is_noise_entity_name(name_norm):
            continue
        conf = clamp_01(item.get("confidence"))

        # --- 集約 ---
        key = (str(t_norm), str(name_norm))
        prev = best_by_key.get(key)
        if prev is None or float(prev.get("confidence") or 0.0) < float(conf):
            best_by_key[key] = {"type": str(t_norm), "name": str(name_raw), "confidence": float(conf)}

    # --- 出力を並べる（安定化: type→name） ---
    out = list(best_by_key.values())
    out.sort(key=lambda x: (str(x.get("type") or ""), str(normalize_entity_name(x.get("name")))))
    return out
