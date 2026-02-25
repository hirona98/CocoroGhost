"""
autonomy runtime blackboard（RAM）

役割:
    - 自発行動の短期ランタイム状態を RAM に保持する。
    - runtime_snapshots の保存/復元と対応する最小限の状態を集約する。
"""

from __future__ import annotations

import threading
from typing import Any


class RuntimeBlackboard:
    """
    自発行動ランタイム状態のインメモリ保持。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "active_intent_ids": [],
            "attention_targets": [],
            "trigger_counts": {},
            "intent_counts": {},
            "worker_now_system_utc_ts": None,
            "last_snapshot_kind": None,
            "last_snapshot_created_at": None,
            "last_recovered_source_snapshot_id": None,
            "last_recovered_system_utc_ts": None,
            "last_recovered_domain_utc_ts": None,
        }

    @staticmethod
    def _normalize_attention_targets(attention_targets_raw: Any) -> list[dict[str, Any]]:
        """
        attention_targets を最小構造へ正規化する。
        """

        # --- list[dict] 以外は空 ---
        if not isinstance(attention_targets_raw, list):
            return []

        # --- 重複排除しつつ上位だけ残す ---
        out: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for item in list(attention_targets_raw):
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("type") or "").strip()
            value = str(item.get("value") or "").strip()
            if not target_type or not value:
                continue
            key = (target_type, value)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(
                {
                    "type": str(target_type),
                    "value": str(value),
                    "weight": float(item.get("weight") or 0.0),
                    "updated_at": (
                        int(item.get("updated_at"))
                        if item.get("updated_at") is not None
                        else None
                    ),
                }
            )
            if len(out) >= 24:
                break
        return out

    def apply_snapshot_payload(self, *, snapshot_kind: str, payload: dict[str, Any], created_at: int) -> None:
        """
        snapshot payload を RAM に反映する。
        """

        # --- payload を最小構造へ正規化 ---
        active_ids_raw = payload.get("active_intent_ids") if isinstance(payload, dict) else []
        active_ids: list[str] = []
        seen: set[str] = set()
        for x in list(active_ids_raw or []):
            s = str(x or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            active_ids.append(s)

        trigger_counts = payload.get("trigger_counts") if isinstance(payload.get("trigger_counts"), dict) else {}
        intent_counts = payload.get("intent_counts") if isinstance(payload.get("intent_counts"), dict) else {}
        attention_targets = self._normalize_attention_targets(payload.get("attention_targets"))
        worker_now = payload.get("worker_now_system_utc_ts")

        # --- RAMへ反映 ---
        with self._lock:
            self._data["active_intent_ids"] = list(active_ids)
            self._data["attention_targets"] = list(attention_targets)
            self._data["trigger_counts"] = dict(trigger_counts)
            self._data["intent_counts"] = dict(intent_counts)
            self._data["worker_now_system_utc_ts"] = (int(worker_now) if worker_now is not None else None)
            self._data["last_snapshot_kind"] = str(snapshot_kind)
            self._data["last_snapshot_created_at"] = int(created_at)

    def mark_recovered(
        self,
        *,
        source_snapshot_id: int | None,
        active_intent_ids: list[str],
        now_system_ts: int,
        now_domain_ts: int,
    ) -> None:
        """
        startup_recovered の復元情報を RAM に反映する。
        """

        # --- active ids を正規化 ---
        active_ids: list[str] = []
        seen: set[str] = set()
        for x in list(active_intent_ids or []):
            s = str(x or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            active_ids.append(s)

        # --- RAMへ反映 ---
        with self._lock:
            self._data["active_intent_ids"] = list(active_ids)
            self._data["last_recovered_source_snapshot_id"] = (
                int(source_snapshot_id) if source_snapshot_id is not None else None
            )
            self._data["last_recovered_system_utc_ts"] = int(now_system_ts)
            self._data["last_recovered_domain_utc_ts"] = int(now_domain_ts)

    def merge_attention_targets(self, *, items: list[dict[str, Any]], now_system_ts: int) -> None:
        """
        attention_targets を上書きマージする。

        方針:
            - type/value をキーにマージする。
            - 重みは新値優先、updated_at は now_system_ts に更新する。
            - 上位件数だけ保持して短期状態として扱う。
        """

        # --- 入力を最小構造へ正規化 ---
        normalized_new = self._normalize_attention_targets(items)
        if not normalized_new:
            return

        with self._lock:
            existing = self._normalize_attention_targets(self._data.get("attention_targets"))
            merged_map: dict[tuple[str, str], dict[str, Any]] = {}

            # --- 既存を先に入れる ---
            for item in existing:
                key = (str(item.get("type") or ""), str(item.get("value") or ""))
                if not key[0] or not key[1]:
                    continue
                merged_map[key] = dict(item)

            # --- 新規/更新を上書き ---
            for item in normalized_new:
                key = (str(item.get("type") or ""), str(item.get("value") or ""))
                if not key[0] or not key[1]:
                    continue
                merged_map[key] = {
                    "type": str(key[0]),
                    "value": str(key[1]),
                    "weight": float(item.get("weight") or 0.0),
                    "updated_at": int(now_system_ts),
                }

            # --- 並び替え（weight desc, updated_at desc）で短く保つ ---
            merged_items = list(merged_map.values())
            merged_items.sort(
                key=lambda x: (
                    -float(x.get("weight") or 0.0),
                    -int(x.get("updated_at") or 0),
                )
            )
            self._data["attention_targets"] = list(merged_items[:24])

    def snapshot(self) -> dict[str, Any]:
        """
        現在の RAM 状態をコピーして返す。
        """

        with self._lock:
            return dict(self._data)


_runtime_blackboard = RuntimeBlackboard()


def get_runtime_blackboard() -> RuntimeBlackboard:
    """RuntimeBlackboard のシングルトンを返す。"""

    return _runtime_blackboard
