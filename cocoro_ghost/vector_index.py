"""
ベクトル索引（vec_items）まわりの共通定義。

目的:
    - vec_items の item_id 生成ルール（kind + entity_id の衝突回避）を1箇所に集約する。
    - kind 定義（event/state/event_affect）を `worker.py` / `memory.py` で重複させない。
"""

from __future__ import annotations


# --- vec_items.kind（整数） ---
VEC_KIND_EVENT = 1
VEC_KIND_STATE = 2
VEC_KIND_EVENT_AFFECT = 3

# --- item_id = kind * stride + entity_id ---
VEC_ID_STRIDE = 10_000_000_000


def vec_item_id(kind: int, entity_id: int) -> int:
    """vec_items の item_id を決定する（kind + entity_id の名前空間衝突を避ける）。"""
    return int(kind) * int(VEC_ID_STRIDE) + int(entity_id)


def vec_entity_id(item_id: int) -> int:
    """vec_items の item_id から entity_id を復元する。"""
    return int(item_id) % int(VEC_ID_STRIDE)

