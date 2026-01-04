"""CocoroGhost 起動スクリプト。

開発時の手動起動を想定する。
配布（PyInstaller）では [cocoro_ghost/entrypoint.py] を使う。
"""

from __future__ import annotations


def main() -> None:
    """uvicorn で FastAPI アプリを起動する。"""

    # --- 依存の import は main 内に寄せて PyInstaller 解析を安定させる ---
    import uvicorn

    uvicorn.run(
        "cocoro_ghost.main:app",
        host="0.0.0.0",
        port=55601,
        reload=False,
    )


if __name__ == "__main__":
    main()
