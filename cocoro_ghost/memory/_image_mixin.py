"""
memory配下: 画像（data URI）処理（mixin）

目的:
    - `/api/chat` / `/api/v2/notification` / `/api/v2/meta-request` で共通な
      data URI画像の検証と要約生成を1箇所に寄せる。
    - 画像そのものは保存せず、要約テキストだけを events に残す設計を維持する。
"""

from __future__ import annotations

import base64
import logging

from fastapi import HTTPException, status

from cocoro_ghost import schemas


logger = logging.getLogger(__name__)

_CHAT_ALLOWED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}
_CHAT_IMAGE_MAX_BYTES_PER_IMAGE = 5 * 1024 * 1024
_CHAT_IMAGE_MAX_BYTES_TOTAL = 20 * 1024 * 1024
_CHAT_DEFAULT_INPUT_TEXT_WHEN_IMAGES_ONLY = "これをみて"


class _ImageMemoryMixin:
    """画像（data URI）処理の実装（mixin）。"""

    def _process_data_uri_images(
        self,
        *,
        raw_images: list[str],
        purpose: str,
    ) -> tuple[list[str], bool, list[str]]:
        """
        data URI 画像を検証し、必要に応じて LLM で要約を生成する。

        Returns:
            (image_summaries, has_valid_image, valid_images_data_uris)
        """
        images_in = [str(x or "") for x in (raw_images or [])]
        if not images_in:
            return ([], False, [])

        # --- data URI -> bytes（検証込み） ---
        images_bytes_by_index: list[bytes | None] = [None for _ in images_in]
        valid_images_bytes: list[bytes] = []
        valid_images_mimes: list[str] = []
        valid_images_index: list[int] = []
        valid_images_data_uris: list[str] = []
        invalid_reasons: list[str] = []

        total_image_bytes = 0
        for idx, data_uri in enumerate(images_in):
            # --- 空は無視（プレースホルダ） ---
            if not str(data_uri or "").strip():
                continue

            # --- data URI 解析 ---
            # NOTE:
            # - 不正画像は「その画像だけ無視」して継続する（入力順の対応は維持）。
            # - ただしサイズ上限（1枚/合計）を超える場合は 400 を返す。
            try:
                mime, b64 = schemas.parse_data_uri_image(data_uri)
            except ValueError as exc:
                # --- data URI が壊れている場合は「その画像だけ無視」する ---
                # NOTE: base64 本文は長く機微になり得るため、ログには出さない。
                invalid_reasons.append("invalid_data_uri")
                logger.warning(
                    "invalid image data uri ignored purpose=%s index=%s len=%s error=%s",
                    str(purpose),
                    int(idx),
                    len(str(data_uri or "")),
                    str(exc),
                )
                continue
            except Exception as exc:  # noqa: BLE001
                # --- 予期しない例外も「その画像だけ無視」し、必ずログに残す ---
                invalid_reasons.append("unexpected_parse_error")
                logger.exception(
                    "unexpected error while parsing image data uri ignored purpose=%s index=%s len=%s error=%s",
                    str(purpose),
                    int(idx),
                    len(str(data_uri or "")),
                    str(exc),
                )
                continue

            if mime not in _CHAT_ALLOWED_IMAGE_MIME_TYPES:
                invalid_reasons.append("mime_not_allowed")
                logger.warning(
                    "image mime not allowed ignored purpose=%s index=%s mime=%s",
                    str(purpose),
                    int(idx),
                    str(mime),
                )
                continue
            try:
                image_bytes = base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001
                invalid_reasons.append("base64_decode_failed")
                logger.warning(
                    "image base64 decode failed ignored purpose=%s index=%s mime=%s error=%s",
                    str(purpose),
                    int(idx),
                    str(mime),
                    str(exc),
                )
                continue

            # --- サイズ上限（1枚） ---
            if len(image_bytes) > int(_CHAT_IMAGE_MAX_BYTES_PER_IMAGE):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "画像サイズが大きすぎます（1枚あたり5MBまで）", "code": "image_too_large"},
                )

            # --- サイズ上限（合計） ---
            total_image_bytes += int(len(image_bytes))
            if int(total_image_bytes) > int(_CHAT_IMAGE_MAX_BYTES_TOTAL):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "画像サイズの合計が大きすぎます（合計20MBまで）", "code": "image_too_large"},
                )

            # --- 有効画像として採用 ---
            images_bytes_by_index[int(idx)] = image_bytes
            valid_images_bytes.append(image_bytes)
            valid_images_mimes.append(str(mime))
            valid_images_index.append(int(idx))
            # NOTE: クライアント配信用に、空白除去済みbase64へ正規化したdata URIを採用する。
            valid_images_data_uris.append(f"data:{mime};base64,{b64}")

        # --- 画像が渡されたのに、有効画像が0枚の場合は異常としてログする ---
        # NOTE:
        # - 仕様上は「不正画像はその画像だけ無視して継続」だが、
        #   全滅はユーザー体験として異常なので観測できるようにする。
        if images_in and not valid_images_bytes:
            reason_stats: dict[str, int] = {}
            for r in invalid_reasons:
                reason_stats[r] = int(reason_stats.get(r, 0)) + 1
            logger.warning(
                "no valid images received purpose=%s images_count=%s reasons=%s",
                str(purpose),
                len(images_in),
                reason_stats,
            )

        # --- 画像要約（詳細）を作る（画像ごと、最大400文字） ---
        # NOTE:
        # - 画像そのものは保存しない（永続化しない）
        # - 要約生成に失敗した画像は "" として継続する
        image_summaries: list[str] = []
        if images_in:
            image_summaries = ["" for _ in images_in]
            if valid_images_bytes:
                # --- 画像要約の最大文字数（TOML設定） ---
                # NOTE:
                # - 画像要約は後段プロンプトへ混ざるため、長すぎると体感と品質が悪化しやすい。
                # - ここは「起動設定」で固定し、運用側で調整できるようにする。
                max_chars = int(self.config_store.toml_config.image_summary_max_chars)  # type: ignore[attr-defined]
                summaries_valid = self.llm_client.generate_image_summary(  # type: ignore[attr-defined]
                    valid_images_bytes,
                    purpose=str(purpose),
                    mime_types=list(valid_images_mimes),
                    max_chars=int(max_chars),
                    best_effort=True,
                )
                for idx2, summary in zip(valid_images_index, summaries_valid, strict=False):
                    image_summaries[int(idx2)] = str(summary or "").strip()

        # --- 有効画像があるのに要約が全て空なら、Vision側の失敗としてログする ---
        # NOTE:
        # - best_effort=True のため、Vision失敗は "" で埋まって処理が継続する。
        # - ここで検出し、後から追えるようにする。
        if valid_images_bytes and not any(str(s or "").strip() for s in (image_summaries or [])):
            logger.warning(
                "image summary generation produced empty summaries purpose=%s valid_images_count=%s",
                str(purpose),
                len(valid_images_bytes),
            )

        has_valid = any(b is not None for b in images_bytes_by_index)
        return (image_summaries, bool(has_valid), list(valid_images_data_uris))


def default_input_text_when_images_only() -> str:
    """画像だけの入力になった場合の既定テキストを返す。"""
    return str(_CHAT_DEFAULT_INPUT_TEXT_WHEN_IMAGES_ONLY)
