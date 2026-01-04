"""実行時パス解決（配布/開発 共通）。

このプロジェクトは Windows 配布（PyInstaller onedir）を主目的にしており、
設定・ログなどの可変データは **exe の隣** に集約する。

DB（settings.db / memory_*.db / reminders.db）は「ユーザーデータ」として扱い、
配布物と分離するため、PyInstaller(frozen) の場合は `..\\UserData\\Ghost\\` に保存する。

方針:
- PyInstaller(frozen) の場合: exe のあるフォルダをアプリルートとする
- 通常実行の場合: CWD をアプリルートとする（start.bat/run.py と相性が良い）
- 例外的に COCORO_GHOST_HOME があれば最優先

"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_app_root_dir() -> Path:
    """アプリのルートディレクトリを返す。

    優先順位:
    1) 環境変数 COCORO_GHOST_HOME
    2) PyInstaller 実行（sys.frozen=True）なら exe のあるフォルダ
    3) それ以外はカレントディレクトリ
    """

    # --- 1) 明示指定（開発/デバッグ向け） ---
    env_home = os.getenv("COCORO_GHOST_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    # --- 2) PyInstaller (frozen) ---
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    # --- 3) 通常実行 ---
    return Path.cwd().resolve()


def get_config_dir() -> Path:
    """設定ディレクトリ（config/）を返し、存在しなければ作成する。"""

    config_dir = get_app_root_dir() / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_data_dir() -> Path:
    """データディレクトリ（data/）を返し、存在しなければ作成する。"""

    data_dir = get_app_root_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_db_dir() -> Path:
        """DB 保存先ディレクトリを返し、存在しなければ作成する。

        方針:
        - PyInstaller(frozen) の場合は、exe の 1 つ上の階層に `UserData/Ghost/` を作り、そこへ保存する。
            つまり exe から見て `..\\UserData\\Ghost\\` となる。
        - 通常実行の場合は、従来どおり app_root 直下の `data/` を使用する。

        NOTE:
        - DB はユーザーデータとして扱い、配布物（exe 隣の data/）と分離する。
        """

        # --- PyInstaller (frozen) だけ配置を変える ---
        if getattr(sys, "frozen", False):
                db_dir = (get_app_root_dir().parent / "UserData" / "Ghost").resolve()
                db_dir.mkdir(parents=True, exist_ok=True)
                return db_dir

        # --- 通常実行は data/ のまま ---
        return get_data_dir()


def get_logs_dir() -> Path:
    """ログディレクトリ（logs/）を返し、存在しなければ作成する。"""

    logs_dir = get_app_root_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_default_config_file_path() -> Path:
    """既定の設定ファイルパス（config/setting.toml）を返す。"""

    return get_config_dir() / "setting.toml"


def resolve_path_under_app_root(path: str | Path) -> Path:
    """相対パスをアプリルート基準の絶対パスに解決する。

    - 絶対パスはそのまま返す
    - 相対パスは app_root / path として解決する
    """

    p = Path(path)
    if p.is_absolute():
        return p
    return (get_app_root_dir() / p).resolve()
