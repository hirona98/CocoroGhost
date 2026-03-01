"""
Tapo Wi-Fi カメラ連携。

役割:
    - Tapo カメラから静止画 1 枚を取得する。
    - `observe_camera` のサーバー直結入力を提供する。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil


_FFMPEG_COMMAND = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
_CAPTURE_WINDOW_SIZE = 50
_PREVIEW_REQUEST = {
    "type": "request",
    "params": {
        "method": "get",
        "preview": {
            "audio": ["default"],
            "channels": [0],
            "resolutions": ["HD"],
        },
    },
}


def _build_preview_payload() -> str:
    """
    Tapo の live preview 要求 payload を返す。
    """

    # --- pytapo の media stream へ送る JSON を最小構成で固定する ---
    return json.dumps(_PREVIEW_REQUEST, separators=(",", ":"))


def _require_ffmpeg_path() -> str:
    """
    ffmpeg 実行ファイルのパスを返す。
    """

    # --- 1フレームを JPEG 化するため ffmpeg を必須にする ---
    ffmpeg_path = shutil.which(_FFMPEG_COMMAND)
    if not ffmpeg_path:
        raise RuntimeError(f"{_FFMPEG_COMMAND} が見つかりません。")
    return str(ffmpeg_path)


def _load_tapo_class():
    """
    pytapo の Tapo クラスを遅延 import して返す。
    """

    # --- camera 機能を使う時だけ依存を読む ---
    try:
        from pytapo import Tapo
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pytapo がインストールされていません。") from exc
    return Tapo


async def _capture_tapo_camera_still_image_async(
    *,
    host: str,
    username: str,
    password: str,
    cloud_password: str,
    timeout_seconds: float,
) -> bytes:
    """
    非同期で Tapo カメラの静止画 1 枚を取得する。
    """

    # --- 依存と設定を確定する ---
    ffmpeg_path = _require_ffmpeg_path()
    Tapo = _load_tapo_class()
    timeout_value = float(max(1.0, float(timeout_seconds)))

    # --- Tapo クライアントを生成する ---
    camera = Tapo(
        str(host),
        str(username),
        str(password),
        cloudPassword=str(cloud_password),
    )
    media_session = camera.getMediaSession()
    media_session.set_window_size(_CAPTURE_WINDOW_SIZE)
    process: asyncio.subprocess.Process | None = None
    image_task: asyncio.Task[bytes] | None = None
    stderr_task: asyncio.Task[bytes] | None = None

    try:
        # --- ffmpeg で MPEG-TS から JPEG 1枚へ変換する ---
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-loglevel",
            "error",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-map",
            "0:v:0",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("ffmpeg パイプの初期化に失敗しました。")

        # --- 標準出力/標準エラーを先に待機させる ---
        image_task = asyncio.create_task(process.stdout.read())
        stderr_task = asyncio.create_task(process.stderr.read())

        # --- Tapo live preview を ffmpeg へ流す ---
        async with media_session:
            payload = _build_preview_payload()
            async for resp in media_session.transceive(payload, no_data_timeout=timeout_value):
                if resp.mimetype != "video/mp2t":
                    continue
                process.stdin.write(resp.plaintext)
                await process.stdin.drain()
                await asyncio.sleep(0)
                if image_task.done():
                    break

        # --- 入力を閉じて ffmpeg の終了を促す ---
        process.stdin.close()

        # --- JPEG を受け取り、ffmpeg の終了を待つ ---
        image_bytes = await asyncio.wait_for(image_task, timeout=timeout_value)
        stderr_bytes = await asyncio.wait_for(stderr_task, timeout=timeout_value)
        return_code = await asyncio.wait_for(process.wait(), timeout=timeout_value)
    finally:
        # --- 残存プロセス/タスク/接続を必ず閉じる ---
        if image_task is not None and not image_task.done():
            image_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await image_task
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        with contextlib.suppress(Exception):
            camera.close()

    # --- 取得結果を検証する ---
    if return_code != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            raise RuntimeError(f"ffmpeg が失敗しました（{stderr_text}）。")
        raise RuntimeError("ffmpeg が失敗しました。")
    if not image_bytes:
        raise RuntimeError("Tapo カメラ画像を取得できませんでした。")
    return bytes(image_bytes)


def capture_tapo_camera_still_image(
    *,
    host: str,
    username: str,
    password: str,
    cloud_password: str,
    timeout_seconds: float,
) -> bytes:
    """
    Tapo カメラから静止画 1 枚を同期取得する。
    """

    # --- 必須設定を検証する ---
    host_value = str(host or "").strip()
    username_value = str(username or "").strip()
    password_value = str(password or "")
    if not host_value:
        raise RuntimeError("tapo_camera_host が未設定です。")
    if not username_value:
        raise RuntimeError("tapo_camera_username が未設定です。")
    if not password_value:
        raise RuntimeError("tapo_camera_password が未設定です。")

    # --- 同期呼び出しから専用イベントループで実行する ---
    return asyncio.run(
        _capture_tapo_camera_still_image_async(
            host=host_value,
            username=username_value,
            password=password_value,
            cloud_password=str(cloud_password or ""),
            timeout_seconds=float(timeout_seconds),
        )
    )
