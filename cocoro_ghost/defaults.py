"""Application defaults.

These defaults are applied when initializing `settings.db`.
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

