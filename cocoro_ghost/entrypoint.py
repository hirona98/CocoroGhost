"""PyInstaller/配布向けのエントリポイント。

設計意図:
- PyInstaller の spec から直接参照できる「単一の起動点」を用意する
- ここでは *保存先ディレクトリ* を確実に作成し、起動に必要な前提を揃える
- uvicorn の起動はプログラムから行う（CLI依存を減らす）
"""

from __future__ import annotations


def main() -> None:
    """配布版のサーバー起動処理。"""

    # --- 先にディレクトリを確実に作る（初回起動時の事故防止） ---
    from cocoro_ghost.infra import paths

    paths.get_config_dir()
    paths.get_data_dir()
    paths.get_db_dir()
    paths.get_logs_dir()

    # --- TLS（自己署名）を用意する ---
    # NOTE: LAN 内でも HTTPS を必須にする。
    from cocoro_ghost.infra.tls import ensure_self_signed_tls_files

    cert_path, key_path = ensure_self_signed_tls_files()

    # --- 設定ファイルが無い場合は、案内して終了 ---
    # 初回起動時に stacktrace を出すよりも、ユーザーが取るべき行動を明確にする。
    config_path = paths.get_default_config_file_path()
    if not config_path.exists():
        print("[CocoroGhost] config/setting.toml が見つかりません。")
        print("[CocoroGhost] config/setting.toml を作成してください。")
        print(f"[CocoroGhost] 期待パス: {config_path}")
        return

    # --- サーバー起動 ---
    # NOTE: uvicorn に文字列を渡すと PyInstaller が依存を拾えないことがある。
    # app を直接 import して渡すことで、配布物への取り込みを確実にする。
    from cocoro_ghost.main import app

    # --- setting.toml から待受ポートを取得する ---
    from cocoro_ghost.config import load_config

    toml_config = load_config(config_path)

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=toml_config.cocoro_ghost_port,
        reload=False,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )


if __name__ == "__main__":
    main()
