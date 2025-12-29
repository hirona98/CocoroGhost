"""
アプリケーションのデフォルト値

settings.db初期化時に適用されるデフォルト設定。
除外キーワードなど、初期状態で設定される値を定義する。
"""

from __future__ import annotations

import json

DEFAULT_EXCLUDE_KEYWORDS = [
    ".*ログイン.*",
    ".*プライベート.*",
    ".*シークレット.*",
    ".*incognito.*",
    ".*Private.*",
    ".*Password.*",
    ".*Login.*",
]

DEFAULT_EXCLUDE_KEYWORDS_JSON = json.dumps(DEFAULT_EXCLUDE_KEYWORDS, ensure_ascii=False)

