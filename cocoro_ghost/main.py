"""
FastAPI エントリポイント。

目的:
    - `main.py` はアプリ生成の配線だけに限定する。
    - 初期化の詳細は app_bootstrap 配下へ委譲する。
"""

from __future__ import annotations

from fastapi import FastAPI

from cocoro_ghost.app_bootstrap import (
    bootstrap_runtime_config,
    register_http_routes,
    register_lifecycle_hooks,
)


def create_app() -> FastAPI:
    """
    FastAPI アプリを生成する。
    """
    # --- 起動時の設定・DB 初期化を先に完了させる ---
    runtime_config = bootstrap_runtime_config()

    # --- FastAPI 本体を生成し、HTTP 配線と lifecycle を登録する ---
    app = FastAPI(title="CocoroGhost API")
    register_http_routes(app)
    register_lifecycle_hooks(app, runtime_config=runtime_config)
    return app


# --- アプリケーションインスタンスを作成する ---
app = create_app()
