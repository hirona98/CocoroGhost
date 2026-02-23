"""
vision_perception capability。

役割:
    - `observe_screen` / `observe_camera` を実行する。
    - 意図決定は持たず、観測結果を ActionResult として返す。
"""

from __future__ import annotations

import base64
from typing import Any

from cocoro_ghost import common_utils, schemas, vision_bridge
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult
from cocoro_ghost.config import get_config_store
from cocoro_ghost.deps import get_memory_manager
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose


def _canonical_vision_client_id(*, action_payload: dict[str, Any]) -> str:
    """
    単一ユーザー運用の視覚クライアントIDを返す。
    """

    # --- action_payload の明示指定を優先 ---
    target_client_id = str(action_payload.get("target_client_id") or "").strip()
    if target_client_id:
        return str(target_client_id)

    # --- 単一ユーザー運用の正: desktop_watch_target_client_id を使う ---
    cfg = get_config_store().config
    return str(getattr(cfg, "desktop_watch_target_client_id", "") or "").strip()


def execute_vision_perception(
    *,
    llm_client: LlmClient,
    action_type: str,
    action_payload: dict[str, Any],
) -> CapabilityExecutionResult:
    """
    vision_perception を実行する。
    """

    # --- 入力を正規化 ---
    action_type_norm = str(action_type or "").strip()
    payload = dict(action_payload or {})
    target_client_id = _canonical_vision_client_id(action_payload=payload)
    if not target_client_id:
        return CapabilityExecutionResult(
            result_status="failed",
            summary="vision_perception の target_client_id が未設定です。",
            result_payload_json=common_utils.json_dumps(
                {"action_type": str(action_type_norm), "action_payload": payload}
            ),
            useful_for_recall_hint=0,
            next_trigger=None,
        )

    # --- observe_screen は既存 desktop_watch 実装を利用する ---
    if action_type_norm == "observe_screen":
        # --- 例外は failed 結果へ変換し、ジョブ再試行ループに入れない ---
        try:
            mm = get_memory_manager()
            result_code = str(mm.run_desktop_watch_once(target_client_id=str(target_client_id)))
        except Exception as exc:  # noqa: BLE001
            return CapabilityExecutionResult(
                result_status="failed",
                summary=f"画面観測に失敗しました（{str(exc)}）。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_screen",
                        "target_client_id": str(target_client_id),
                        "error": str(exc),
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )
        if result_code == "ok":
            return CapabilityExecutionResult(
                result_status="success",
                summary="画面観測を実行しました。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_screen",
                        "target_client_id": str(target_client_id),
                        "result_code": str(result_code),
                    }
                ),
                useful_for_recall_hint=1,
                next_trigger=None,
            )
        if result_code in {"skipped_idle", "skipped_excluded_window_title"}:
            return CapabilityExecutionResult(
                result_status="no_effect",
                summary=f"画面観測はスキップされました（{result_code}）。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_screen",
                        "target_client_id": str(target_client_id),
                        "result_code": str(result_code),
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )
        return CapabilityExecutionResult(
            result_status="failed",
            summary=f"画面観測に失敗しました（{result_code}）。",
            result_payload_json=common_utils.json_dumps(
                {
                    "action_type": "observe_screen",
                    "target_client_id": str(target_client_id),
                    "result_code": str(result_code),
                }
            ),
            useful_for_recall_hint=0,
            next_trigger=None,
        )

    # --- observe_camera は capture + image summary を ActionResult として返す ---
    if action_type_norm == "observe_camera":
        # --- 例外は failed 結果へ変換し、ジョブ再試行ループに入れない ---
        try:
            resp = vision_bridge.request_capture_and_wait(
                target_client_id=str(target_client_id),
                source="camera",
                purpose="desktop_watch",
                timeout_seconds=5.0,
                timeout_ms=5000,
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityExecutionResult(
                result_status="failed",
                summary=f"カメラ観測に失敗しました（{str(exc)}）。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_camera",
                        "target_client_id": str(target_client_id),
                        "error": str(exc),
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )
        if resp is None:
            return CapabilityExecutionResult(
                result_status="no_effect",
                summary="カメラ観測は取得できませんでした（未接続/タイムアウト）。",
                result_payload_json=common_utils.json_dumps(
                    {"action_type": "observe_camera", "target_client_id": str(target_client_id)}
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )
        if vision_bridge.is_capture_skipped_error(resp.error):
            return CapabilityExecutionResult(
                result_status="no_effect",
                summary=f"カメラ観測はスキップされました（{str(resp.error)}）。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_camera",
                        "target_client_id": str(target_client_id),
                        "error": str(resp.error or ""),
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )
        if resp.error is not None or not list(resp.images or []):
            return CapabilityExecutionResult(
                result_status="failed",
                summary="カメラ観測に失敗しました。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_camera",
                        "target_client_id": str(target_client_id),
                        "error": (str(resp.error) if resp.error is not None else None),
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )

        # --- 画像要約 ---
        images_bytes: list[bytes] = []
        for data_uri in list(resp.images or []):
            b64 = schemas.data_uri_image_to_base64(str(data_uri))
            images_bytes.append(base64.b64decode(b64))
        # --- 画像要約失敗も例外伝播させず failed 結果にする ---
        try:
            descriptions = llm_client.generate_image_summary(
                images_bytes,
                purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_CHAT,
                max_chars=600,
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityExecutionResult(
                result_status="failed",
                summary=f"カメラ画像要約に失敗しました（{str(exc)}）。",
                result_payload_json=common_utils.json_dumps(
                    {
                        "action_type": "observe_camera",
                        "target_client_id": str(target_client_id),
                        "error": str(exc),
                        "image_count": int(len(images_bytes)),
                    }
                ),
                useful_for_recall_hint=0,
                next_trigger=None,
            )
        detail_text = "\n\n".join([str(x).strip() for x in descriptions if str(x or "").strip()]).strip()
        if not detail_text:
            detail_text = "カメラ画像の要約を取得しました。"

        return CapabilityExecutionResult(
            result_status="success",
            summary="カメラ観測を実行しました。",
            result_payload_json=common_utils.json_dumps(
                {
                    "action_type": "observe_camera",
                    "target_client_id": str(target_client_id),
                    "camera_detail": str(detail_text),
                    "image_count": int(len(list(resp.images or []))),
                    "client_context": (dict(resp.client_context) if isinstance(resp.client_context, dict) else None),
                }
            ),
            useful_for_recall_hint=1,
            next_trigger=None,
        )

    # --- 未対応 action_type ---
    return CapabilityExecutionResult(
        result_status="failed",
        summary=f"unsupported vision action_type: {action_type_norm}",
        result_payload_json=common_utils.json_dumps(
            {"action_type": str(action_type_norm), "action_payload": payload}
        ),
        useful_for_recall_hint=0,
        next_trigger=None,
    )
