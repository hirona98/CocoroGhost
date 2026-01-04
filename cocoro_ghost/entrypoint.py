"""PyInstaller/配布向けのエントリポイント。

設計意図:
- PyInstaller の spec から直接参照できる「単一の起動点」を用意する
- ここでは *保存先ディレクトリ* を確実に作成し、起動に必要な前提を揃える
- uvicorn の起動はプログラムから行う（CLI依存を減らす）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> None:
    """配布版のサーバー起動処理。"""

    # --- 先にディレクトリを確実に作る（初回起動時の事故防止） ---
    from cocoro_ghost import paths

    config_dir = paths.get_config_dir()
    paths.get_data_dir()
    paths.get_logs_dir()

    # --- 設定テンプレートを exe 隣へ配置する ---
    # PyInstaller(on dir) では datas が _internal 配下になることがあるため、
    # 起動時にユーザーが編集できる場所（exe隣の config/）へコピーしておく。
    try:
        external_example = config_dir / "setting.toml.example"
        if not external_example.exists():
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                bundled_example = Path(meipass) / "config" / "setting.toml.example"
                if bundled_example.exists():
                    shutil.copy2(bundled_example, external_example)
    except Exception:
        # テンプレは利便性のための補助なので、失敗しても起動は継続する。
        pass

    # --- 設定ファイルが無い場合は、案内して終了 ---
    # 初回起動時に stacktrace を出すよりも、ユーザーが取るべき行動を明確にする。
    config_path = paths.get_default_config_file_path()
    if not config_path.exists():
        print("[CocoroGhost] config/setting.toml が見つかりません。")
        print("[CocoroGhost] config/setting.toml.example を参考に作成してください。")
        print(f"[CocoroGhost] 期待パス: {config_path}")
        return

    # --- サーバー起動 ---
    # NOTE: uvicorn に文字列を渡すと PyInstaller が依存を拾えないことがある。
    # app を直接 import して渡すことで、配布物への取り込みを確実にする。
    from cocoro_ghost.main import app

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=55601, reload=False)


if __name__ == "__main__":
    main()