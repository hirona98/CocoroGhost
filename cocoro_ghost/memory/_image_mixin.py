"""
memory配下: 画像（data URI）処理（mixin）

目的:
    - `/api/chat` / `/api/v2/notification` / `/api/v2/meta-request` で共通な
      data URI画像の検証と要約生成を1箇所に寄せる。
    - 画像そのものは保存せず、要約テキストだけを events に残す設計を維持する。
"""

from __future__ import annotations

import base64

from fastapi import HTTPException, status

from cocoro_ghost import schemas


_CHAT_ALLOWED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}
_CHAT_IMAGE_MAX_BYTES_PER_IMAGE = 5 * 1024 * 1024
_CHAT_IMAGE_MAX_BYTES_TOTAL = 20 * 1024 * 1024
_CHAT_IMAGE_SUMMARY_MAX_CHARS = 400
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
                mime = schemas.data_uri_image_to_mime(data_uri)
                b64 = schemas.data_uri_image_to_base64(data_uri)
            except Exception:  # noqa: BLE001
                continue

            if mime not in _CHAT_ALLOWED_IMAGE_MIME_TYPES:
                continue
            try:
                image_bytes = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
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

        # --- 画像要約（詳細）を作る（画像ごと、最大400文字） ---
        # NOTE:
        # - 画像そのものは保存しない（永続化しない）
        # - 要約生成に失敗した画像は "" として継続する
        image_summaries: list[str] = []
        if images_in:
            image_summaries = ["" for _ in images_in]
            if valid_images_bytes:
                summaries_valid = self.llm_client.generate_image_summary(  # type: ignore[attr-defined]
                    valid_images_bytes,
                    purpose=str(purpose),
                    mime_types=list(valid_images_mimes),
                    max_chars=int(_CHAT_IMAGE_SUMMARY_MAX_CHARS),
                    best_effort=True,
                )
                for idx2, summary in zip(valid_images_index, summaries_valid, strict=False):
                    image_summaries[int(idx2)] = str(summary or "").strip()

        has_valid = any(b is not None for b in images_bytes_by_index)
        return (image_summaries, bool(has_valid), list(valid_images_data_uris))


def default_input_text_when_images_only() -> str:
    """画像だけの入力になった場合の既定テキストを返す。"""
    return str(_CHAT_DEFAULT_INPUT_TEXT_WHEN_IMAGES_ONLY)
