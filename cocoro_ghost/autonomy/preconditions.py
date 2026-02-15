"""
自律ループの precondition 評価。

役割:
- precondition 語彙を1箇所で管理する。
- Tacticalize/Execute 間で同一ルールの評価を行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


# --- 既知 precondition 語彙 ---
PRECONDITION_OPERATION_AVAILABLE = "operation_available"
PRECONDITION_URL_PRESENT = "url_present"
PRECONDITION_SOURCE_URL_PRESENT = "source_url_present"
PRECONDITION_SOURCE_TEXT_PRESENT = "source_text_present"


class _OperationResolverProtocol(Protocol):
    """operation 可用性判定に必要な registry 契約。"""

    def resolve_operation(self, *, capability_id: str, operation: str) -> Any:
        """operation descriptor を返す。"""

    def resolve_adapter(self, *, capability_id: str) -> Any:
        """adapter を返す。"""


@dataclass(frozen=True)
class PreconditionEvaluationResult:
    """precondition 評価結果。"""

    satisfied: bool
    failed_precondition: str | None = None


def normalize_preconditions(*, preconditions: list[str]) -> list[str]:
    """precondition 配列を正規化する。"""

    # --- 空要素除去 + 順序維持で重複排除 ---
    out: list[str] = []
    seen: set[str] = set()
    for item in list(preconditions or []):
        token = str(item or "").strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def build_effective_preconditions(*, preconditions: list[str]) -> list[str]:
    """共通 precondition を含む評価対象配列を返す。"""

    # --- 明示 precondition を正規化 ---
    normalized = normalize_preconditions(preconditions=list(preconditions or []))

    # --- operation 可用性を必須 precondition として先頭へ追加 ---
    out = [PRECONDITION_OPERATION_AVAILABLE]
    for token in list(normalized):
        if token == PRECONDITION_OPERATION_AVAILABLE:
            continue
        out.append(token)
    return out


def _is_operation_available(
    *,
    capability_id: str,
    operation: str,
    registry: _OperationResolverProtocol,
) -> bool:
    """capability/operation が実行可能かを判定する。"""

    # --- descriptor 解決を試行 ---
    try:
        _ = registry.resolve_operation(capability_id=str(capability_id), operation=str(operation))
    except Exception:  # noqa: BLE001
        return False

    # --- adapter 解決を試行 ---
    try:
        _ = registry.resolve_adapter(capability_id=str(capability_id))
    except Exception:  # noqa: BLE001
        return False
    return True


def evaluate_preconditions(
    *,
    preconditions: list[str],
    capability_id: str,
    operation: str,
    input_payload: dict[str, Any],
    registry: _OperationResolverProtocol,
) -> PreconditionEvaluationResult:
    """precondition 配列を評価する。"""

    # --- 正規化済み precondition を順に評価 ---
    for token in list(normalize_preconditions(preconditions=list(preconditions or []))):
        if token == PRECONDITION_OPERATION_AVAILABLE:
            if not _is_operation_available(
                capability_id=str(capability_id),
                operation=str(operation),
                registry=registry,
            ):
                return PreconditionEvaluationResult(satisfied=False, failed_precondition=str(token))
            continue

        if token == PRECONDITION_URL_PRESENT:
            if not str(input_payload.get("url") or "").strip():
                return PreconditionEvaluationResult(satisfied=False, failed_precondition=str(token))
            continue

        if token == PRECONDITION_SOURCE_URL_PRESENT:
            if not str(input_payload.get("source_url") or "").strip():
                return PreconditionEvaluationResult(satisfied=False, failed_precondition=str(token))
            continue

        if token == PRECONDITION_SOURCE_TEXT_PRESENT:
            if not str(input_payload.get("source_text") or "").strip():
                return PreconditionEvaluationResult(satisfied=False, failed_precondition=str(token))
            continue

        # --- 未知 precondition は fail-fast で不成立 ---
        return PreconditionEvaluationResult(satisfied=False, failed_precondition=str(token))

    return PreconditionEvaluationResult(satisfied=True, failed_precondition=None)

