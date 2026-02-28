"""
memory配下: 外部入力（notification/meta_request/vision/reminder/desktop_watch）処理（mixin）

目的:
    - chat 以外の入口を `MemoryManager` から分離し、責務を明確化する。
    - 入口ごとに「どのように events を作るか」「何を配信するか」を固定する。
"""

from __future__ import annotations

import base64

from fastapi import BackgroundTasks, HTTPException, status

from cocoro_ghost import common_utils
from cocoro_ghost import schemas
from cocoro_ghost.llm import prompt_builders
from cocoro_ghost.storage.db import memory_session_scope
from cocoro_ghost.llm.client import LlmRequestPurpose
from cocoro_ghost.memory._image_mixin import default_input_text_when_images_only
from cocoro_ghost.memory._utils import now_utc_ts
from cocoro_ghost.storage.memory_models import Event
from cocoro_ghost.runtime import event_stream
from cocoro_ghost import vision_bridge


def _format_hhmm_to_time_jp(hhmm: str) -> str:
    """
    HH:MM を「H時MM分」に変換する。

    NOTE:
        - 異常値の場合は「時刻」を返し、処理を継続する（発火を落とさない）。
    """
    s = str(hhmm or "").strip()
    try:
        parts = s.split(":")
        if len(parts) != 2:
            return "時刻"
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return "時刻"
        return f"{hour}時{minute:02d}分"
    except Exception:  # noqa: BLE001
        return "時刻"


class _ExternalMemoryMixin:
    """外部入力経路の実装（mixin）。"""

    def handle_notification(self, request: schemas.NotificationRequest, *, background_tasks: BackgroundTasks) -> None:
        """通知を受け取り、出来事ログとして保存し、イベントとして配信する。"""

        # --- 設定 ---
        cfg = self.config_store.config  # type: ignore[attr-defined]
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = now_utc_ts()

        # --- 入力 ---
        client_id = str(request.client_id or "").strip()
        target_client_id = client_id if client_id else None
        source_system = str(request.source_system or "").strip() or "外部システム"
        text_in = str(request.text or "").strip()
        raw_images = list(request.images or [])

        # --- 画像付き通知（/api/chat と同様の流れ） ---
        image_summaries, has_valid_image, valid_images_data_uris = self._process_data_uri_images(  # type: ignore[attr-defined]
            raw_images=[str(x or "") for x in raw_images],
            purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_NOTIFICATION,
        )
        non_empty_summaries = [s for s in image_summaries if str(s or "").strip()]

        # --- text が空の場合は、画像の有無で扱いを分ける ---
        if not text_in:
            if bool(has_valid_image):
                text_in = default_input_text_when_images_only()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "text が空で、かつ有効な画像がありません", "code": "invalid_request"},
                )

        # --- LLMで「通知を受け取ったこと」をユーザーへ伝える発話を生成 ---
        # NOTE:
        # - 通知はユーザー発話ではないため、「教えてくれてありがとう」等が出ないように
        #   通知専用の user prompt でガードする。
        system_prompt = prompt_builders.reply_system_prompt(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            second_person_label=cfg.second_person_label,
        )
        user_prompt = prompt_builders.notification_user_prompt(
            source_system=str(source_system),
            text=str(text_in),
            has_any_valid_image=bool(has_valid_image),
            second_person_label=cfg.second_person_label,
        )

        # --- 画像要約（内部用）を注入（本文に出さない） ---
        conversation: list[dict[str, str]] = []
        if non_empty_summaries:
            conversation.append(
                {
                    "role": "assistant",
                    "content": "\n".join(
                        [
                            "<<INTERNAL_CONTEXT>>",
                            "ImageSummaries（通知・内部用。本文に出力しない）:",
                            "\n".join([str(s) for s in non_empty_summaries]),
                        ]
                    ).strip(),
                }
            )
        conversation.append({"role": "user", "content": user_prompt})

        resp = self.llm_client.generate_reply_response(  # type: ignore[attr-defined]
            system_prompt=system_prompt,
            conversation=conversation,
            purpose=LlmRequestPurpose.SYNC_NOTIFICATION,
            stream=False,
        )
        message = common_utils.first_choice_content(resp).strip()

        # --- 保存用テキスト（検索/記憶用途） ---
        user_text = "\n".join([f"通知: {source_system}", text_in]).strip()

        # --- events に保存 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=(str(target_client_id) if target_client_id else None),
                source="notification",
                user_text=user_text,
                assistant_text=message,
                image_summaries_json=(common_utils.json_dumps(image_summaries) if raw_images else None),
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信 ---
        event_stream.publish(
            type="notification",
            event_id=int(event_id),
            data={
                "system_text": f"[{source_system}] {text_in}",
                "message": message,
                "images": list(valid_images_data_uris),
            },
            target_client_id=(str(target_client_id) if target_client_id else None),
        )

        # --- 非同期: 埋め込み更新 ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: アシスタント本文要約（選別入力の高速化） ---
        background_tasks.add_task(
            self._enqueue_event_assistant_summary_job,  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        background_tasks.add_task(
            self._enqueue_write_plan_job,  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def handle_meta_request(self, request: schemas.MetaRequestRequest, *, background_tasks: BackgroundTasks) -> None:
        """メタ依頼を受け取り、外部要求は保存せず、能動メッセージの結果だけを保存する。"""

        # --- 設定 ---
        cfg = self.config_store.config  # type: ignore[attr-defined]
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = now_utc_ts()

        # --- 入力（永続化しない） ---
        instruction = str(request.instruction or "").strip()
        payload_text = str(request.payload_text or "").strip()
        raw_images = list(request.images or [])

        # --- 画像付きメタ依頼（/api/chat と同様の流れ） ---
        image_summaries, has_valid_image, _valid_images_data_uris = self._process_data_uri_images(  # type: ignore[attr-defined]
            raw_images=[str(x or "") for x in raw_images],
            purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_META_REQUEST,
        )
        non_empty_summaries = [s for s in image_summaries if str(s or "").strip()]

        # --- instruction/payload_text が空の場合は、画像の有無で扱いを分ける ---
        if not instruction and not payload_text:
            if bool(has_valid_image):
                instruction = default_input_text_when_images_only()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "instruction/payload_text が空で、かつ有効な画像がありません", "code": "invalid_request"},
                )

        # --- LLMで能動メッセージを生成（外部要求だとは悟らせない） ---
        system_prompt = prompt_builders.reply_system_prompt(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            second_person_label=cfg.second_person_label,
        )
        user_prompt = prompt_builders.meta_request_user_prompt(
            second_person_label=cfg.second_person_label,
            instruction=instruction,
            payload_text=payload_text,
        )

        # --- 画像要約（内部用）を注入（本文に出さない） ---
        conversation: list[dict[str, str]] = []
        if non_empty_summaries:
            conversation.append(
                {
                    "role": "assistant",
                    "content": "\n".join(
                        [
                            "<<INTERNAL_CONTEXT>>",
                            "画像要約（内部用。本文に出力しない）:",
                            "\n".join([str(s) for s in non_empty_summaries]),
                        ]
                    ).strip(),
                }
            )
        conversation.append({"role": "user", "content": user_prompt})

        resp = self.llm_client.generate_reply_response(  # type: ignore[attr-defined]
            system_prompt=system_prompt,
            conversation=conversation,
            purpose=LlmRequestPurpose.SYNC_META_REQUEST,
            stream=False,
        )
        message = common_utils.first_choice_content(resp).strip()

        # --- events に保存（sourceで区別） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="meta_proactive",
                user_text=None,
                assistant_text=message,
                image_summaries_json=(common_utils.json_dumps(image_summaries) if raw_images else None),
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信 ---
        event_stream.publish(
            type="meta-request",
            event_id=int(event_id),
            data={"message": message},
            target_client_id=None,
        )

        # --- 非同期: 埋め込み更新 ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        background_tasks.add_task(
            self._enqueue_write_plan_job,  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def handle_vision_capture_response(self, request: schemas.VisionCaptureResponseV2Request) -> None:
        """視覚のcapture-responseを受け取り、待機中の要求へ紐づけ、画像説明を出来事ログへ保存する。"""

        # --- 画像説明テキストの最大長（文字） ---
        # NOTE:
        # - vision_detail は出来事ログとして保持するため、極端な長文は避ける。
        # - ここでは「画像説明（詳細）」の上限を 600 文字に揃える。
        image_detail_max_chars = 600

        # --- まずは待機中の要求へ紐づける（タイムアウト済みなら何もしない） ---
        ok = vision_bridge.fulfill_capture_response(
            vision_bridge.VisionCaptureResponse(
                request_id=str(request.request_id),
                client_id=str(request.client_id),
                images=list(request.images or []),
                client_context=(request.client_context if request.client_context is not None else None),
                error=(str(request.error).strip() if request.error is not None else None),
            )
        )
        if not ok:
            return

        # --- 画像は保存しない。images があれば詳細説明テキストを生成して events に残す ---
        if not request.images:
            return
        if request.error is not None:
            return

        # --- data URI -> bytes ---
        images_bytes: list[bytes] = []
        for s in list(request.images or []):
            b64 = schemas.data_uri_image_to_base64(s)
            images_bytes.append(base64.b64decode(b64))

        # --- LLMで詳細説明 ---
        descriptions = self.llm_client.generate_image_summary(  # type: ignore[attr-defined]
            images_bytes,
            purpose=LlmRequestPurpose.SYNC_IMAGE_DETAIL,
            max_chars=int(image_detail_max_chars),
        )
        detail_text = "\n\n".join([d.strip() for d in descriptions if str(d or "").strip()]).strip()
        if not detail_text:
            return

        # --- events に保存（画像は保持しない） ---
        cfg = self.config_store.config  # type: ignore[attr-defined]
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=str(request.client_id),
                source="vision_detail",
                user_text=detail_text,
                assistant_text=None,
                entities_json="[]",
                client_context_json=(
                    common_utils.json_dumps(request.client_context) if request.client_context is not None else None
                ),
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # NOTE: vision_detail はUIへ通知する必要がないため events/stream へは配信しない。

        # --- 非同期: 埋め込み更新（次ターン以降で参照できる） ---
        self._enqueue_event_embedding_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: アシスタント本文要約（選別入力の高速化） ---
        self._enqueue_event_assistant_summary_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )
        self._enqueue_autonomy_event_trigger(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            source="vision_detail",
        )

    def run_reminder_once(self, *, hhmm: str, content: str) -> None:
        """
        リマインダーを1件発火し、イベントストリームへブロードキャスト配信する。

        仕様:
        - events/stream の reminder は data.message のみを送る。
        - 宛先（target_client_id）はリマインダー機能では扱わない。
        """

        # --- 設定 ---
        cfg = self.config_store.config  # type: ignore[attr-defined]
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = now_utc_ts()

        # --- LLMで文面を生成（人格を必ず反映する） ---
        time_jp = _format_hhmm_to_time_jp(str(hhmm))
        content_one_line = " ".join(
            [x.strip() for x in str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if x.strip()]
        ).strip()

        system_prompt = prompt_builders.reply_system_prompt(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            second_person_label=cfg.second_person_label,
        )
        user_prompt = prompt_builders.reminder_user_prompt(
            time_jp=str(time_jp),
            content=str(content_one_line),
            second_person_label=cfg.second_person_label,
        )

        resp = self.llm_client.generate_reply_response(  # type: ignore[attr-defined]
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.SYNC_REMINDER,
            stream=False,
        )
        message = common_utils.first_choice_content(resp).strip()

        # --- events に保存 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="reminder",
                user_text=str(content),
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信（バッファしない） ---
        event_stream.publish(
            type="reminder",
            event_id=int(event_id),
            data={
                "message": message,
            },
            target_client_id=None,
        )

        # --- 非同期: 埋め込み更新 ---
        self._enqueue_event_embedding_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: アシスタント本文要約（選別入力の高速化） ---
        self._enqueue_event_assistant_summary_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )
        # NOTE:
        # - autonomy trigger は reminders_service（Scheduler Policy）側で `time` として投入する。
        # - ここでは reminder event の保存/配信だけを責務にする。

    def run_desktop_watch_once(self, *, target_client_id: str) -> str:
        """デスクトップウォッチを1回実行する（結果コードを返す）。"""

        # --- 設定 ---
        cfg = self.config_store.config  # type: ignore[attr-defined]
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)

        # --- 画像説明テキストの最大長（文字） ---
        # NOTE:
        # - デスクトップウォッチの画像説明は「画面の材料」だが、長すぎると後段プロンプトのノイズになりやすい。
        # - ここでは vision 側の出力を 600 文字に制限し、扱いやすい粒度に揃える。
        image_detail_max_chars = 600

        # --- 視覚要求（命令） ---
        resp = vision_bridge.request_capture_and_wait(
            target_client_id=str(target_client_id),
            source="desktop",
            purpose="desktop_watch",
            timeout_seconds=5.0,
            timeout_ms=5000,
        )
        if resp is None:
            return "skipped_idle"
        if vision_bridge.is_capture_skipped_idle(resp.error):
            return "skipped_idle"
        if vision_bridge.is_capture_skipped_excluded_window_title(resp.error):
            return "skipped_excluded_window_title"
        if resp.error is not None:
            return "failed"
        if not resp.images:
            return "failed"

        # --- 画像説明（詳細） ---
        images_bytes: list[bytes] = []
        for s in list(resp.images or []):
            b64 = schemas.data_uri_image_to_base64(s)
            images_bytes.append(base64.b64decode(b64))
        descriptions = self.llm_client.generate_image_summary(  # type: ignore[attr-defined]
            images_bytes,
            purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_DESKTOP_WATCH,
            max_chars=int(image_detail_max_chars),
        )
        detail_text = "\n\n".join([d.strip() for d in descriptions if str(d or "").strip()]).strip()

        # --- LLMで人格コメントを生成 ---
        system_prompt = prompt_builders.reply_system_prompt(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            second_person_label=cfg.second_person_label,
        )
        internal_context = prompt_builders.desktop_watch_internal_context(
            detail_text=detail_text, client_context=resp.client_context
        )
        user_prompt = prompt_builders.desktop_watch_user_prompt(second_person_label=cfg.second_person_label)
        resp2 = self.llm_client.generate_reply_response(  # type: ignore[attr-defined]
            system_prompt=system_prompt,
            conversation=[
                {"role": "assistant", "content": internal_context},
                {"role": "user", "content": user_prompt},
            ],
            purpose=LlmRequestPurpose.SYNC_DESKTOP_WATCH,
            stream=False,
        )
        message = common_utils.first_choice_content(resp2).strip()

        # --- events に保存（画像は保持しない） ---
        now_ts = now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=str(target_client_id),
                source="desktop_watch",
                user_text=detail_text,
                assistant_text=message,
                entities_json="[]",
                client_context_json=(
                    common_utils.json_dumps(resp.client_context) if resp.client_context is not None else None
                ),
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信（リアルタイム性を優先してバッファしない） ---
        ctx = dict(resp.client_context or {})
        active_app = str(ctx.get("active_app") or "").strip()
        window_title = str(ctx.get("window_title") or "").strip()
        system_text = " ".join([x for x in ["[desktop_watch]", active_app, window_title] if str(x).strip()]).strip()
        event_stream.publish(
            type="desktop_watch",
            event_id=int(event_id),
            data={
                "system_text": system_text,
                "message": message,
            },
            target_client_id=str(target_client_id),
        )

        # --- 埋め込み更新 ---
        self._enqueue_event_embedding_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: アシスタント本文要約（選別入力の高速化） ---
        self._enqueue_event_assistant_summary_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )
        self._enqueue_autonomy_event_trigger(  # type: ignore[attr-defined]
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            source="desktop_watch",
        )
        return "ok"
