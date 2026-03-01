"""
vision_perception capability。

役割:
    - `observe_screen` / `observe_camera` を実行する。
    - 意図決定は持たず、観測結果を ActionResult として返す。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost.core import common_utils
from cocoro_ghost.vision.tapo_camera import capture_tapo_camera_still_image
from cocoro_ghost.autonomy.contracts import CapabilityExecutionResult
from cocoro_ghost.config import get_config_store
from cocoro_ghost.app_bootstrap.dependencies import get_memory_manager
from cocoro_ghost.llm.client import LlmClient, LlmRequestPurpose


def _canonical_desktop_client_id(*, action_payload: dict[str, Any]) -> str:
    """
    単一ユーザー運用のデスクトップ観測クライアントIDを返す。
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

    # --- Capability実行全体で例外を failed 結果へ変換する ---
    try:
        # --- observe_screen は既存 desktop_watch 実装を利用する ---
        if action_type_norm == "observe_screen":
            target_client_id = _canonical_desktop_client_id(action_payload=payload)
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

            mm = get_memory_manager()
            result_code = str(mm.run_desktop_watch_once(target_client_id=str(target_client_id)))
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

        # --- observe_camera は Tapo カメラを直接読み、image summary を ActionResult として返す ---
        if action_type_norm == "observe_camera":
            cfg = get_config_store().config
            image_bytes = capture_tapo_camera_still_image(
                host=str(cfg.tapo_camera_host),
                username=str(cfg.tapo_camera_username),
                password=str(cfg.tapo_camera_password),
                cloud_password=str(cfg.tapo_camera_cloud_password),
                timeout_seconds=float(cfg.image_timeout_seconds),
            )
            descriptions = llm_client.generate_image_summary(
                [bytes(image_bytes)],
                purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_CHAT,
                max_chars=600,
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
                        "camera_detail": str(detail_text),
                        "image_count": 1,
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
    except Exception as exc:  # noqa: BLE001
        return CapabilityExecutionResult(
            result_status="failed",
            summary=f"vision_perception 実行に失敗しました（{str(exc)}）。",
            result_payload_json=common_utils.json_dumps(
                {
                    "action_type": str(action_type_norm),
                    "action_payload": payload,
                    "error": str(exc),
                }
            ),
            useful_for_recall_hint=0,
            next_trigger=None,
        )
