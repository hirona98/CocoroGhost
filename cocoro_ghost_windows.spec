# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec (Windows配布用)

方針:
- onedir を前提にしてトラブルを減らす
- sqlite-vec の loadable extension（vec0）を同梱する
- 設定は exe の隣（config/）に置く（build.bat 側で配置）
- 可変データ（DB/キャッシュ等）は `..\\UserData\\Ghost\\` に保存する（コード側で対応）
"""

from __future__ import annotations

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata


# --- datas/binaries ---
datas = []
# 設定ファイルについて
# - PyInstaller 6 の onedir は、同梱した datas が dist/<app>/_internal 配下に入る。
# - 今回は「exe の隣（dist/<app>/config）」に置きたいので spec では同梱しない。
# - dist/<app>/config/setting.toml は build.bat 側で配置する。

binaries = []
# sqlite-vec の loadable extension（vec0.*）を同梱
binaries += collect_dynamic_libs("sqlite_vec")

# tiktoken は動的に plugin(tiktoken_ext) を探索してエンコーディング定義を登録する。
# PyInstaller だと解析で落ちやすいため、明示的に取り込む。
datas += collect_data_files("tiktoken")
# plugin 検出は importlib.metadata の entry_points に依存するため、dist-info メタデータも同梱する
datas += copy_metadata("tiktoken")

# tiktoken の plugin 検出は pkgutil.iter_modules(tiktoken_ext.__path__) を使う。
# そのため、tiktoken_ext の .py を datas としてファイルシステム上に配置して走査可能にする。
datas += collect_data_files("tiktoken_ext", include_py_files=True)

# litellm は実行時に tokenizer の json を pathlib.Path で開く。
# PyInstaller では自動で拾われないことがあるため、json を明示的に同梱する。
datas += collect_data_files("litellm", includes=["**/*.json"])


# --- hidden imports ---
# 関数内 import など、解析で落ちやすいものを明示
hiddenimports = [
    "cocoro_ghost.main",
    "cocoro_ghost.reminders_models",
    # tiktoken encodings (e.g., cl100k_base)
    "tiktoken_ext.openai_public",
]

# tiktoken_ext 配下は動的に読み込まれるため、サブモジュールも拾っておく
hiddenimports += collect_submodules("tiktoken_ext")


a = Analysis(
    ["cocoro_ghost/entrypoint.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CocoroGhost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CocoroGhost",
)
