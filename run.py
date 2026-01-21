"""CocoroGhost 起動スクリプト。

開発時の手動起動を想定する。
配布（PyInstaller）では [cocoro_ghost/entrypoint.py] を使う。
"""

from __future__ import annotations


def main() -> None:
    """uvicorn で FastAPI アプリを起動する。"""

    # --- 依存の import は main 内に寄せて PyInstaller 解析を安定させる ---
    import uvicorn

    # --- setting.toml から待受ポートを取得する ---
    from cocoro_ghost.config import load_config

    toml_config = load_config()

    # --- TLS（自己署名）を用意する ---
    from cocoro_ghost.tls import ensure_self_signed_tls_files

    cert_path, key_path = ensure_self_signed_tls_files()

    # --- 開発用: コード変更を自動でリロードする ---
    uvicorn.run(
        "cocoro_ghost.main:app",
        host="0.0.0.0",
        port=toml_config.cocoro_ghost_port,
        reload=True,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )


if __name__ == "__main__":
    main()
