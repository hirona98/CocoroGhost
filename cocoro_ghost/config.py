"""設定読み込みとランタイム設定ストア。"""

from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass, field, replace
from typing import List, Optional

import tomli


@dataclass
class Config:
    token: str
    db_url: str
    llm_model: str
    reflection_model: str
    embedding_model: str
    image_model: str
    image_timeout_seconds: int
    log_level: str
    env: str
    max_chat_queue: int
    exclude_keywords: List[str] = field(default_factory=list)
    character_prompt: Optional[str] = None
    intervention_level: Optional[str] = None


class ConfigStore:
    """スレッドセーフではない単純なランタイム設定ストア。"""

    def __init__(self, initial: Config) -> None:
        self._config = initial
        self._lock = threading.Lock()

    @property
    def config(self) -> Config:
        return self._config

    def update(self, *, exclude_keywords: Optional[List[str]] = None, character_prompt: Optional[str] = None, intervention_level: Optional[str] = None) -> Config:
        with self._lock:
            cfg = self._config
            updated = replace(cfg)
            if exclude_keywords is not None:
                updated.exclude_keywords = exclude_keywords
            if character_prompt is not None:
                updated.character_prompt = character_prompt
            if intervention_level is not None:
                updated.intervention_level = intervention_level
            self._config = updated
            return updated


def _require(config_dict: dict, key: str) -> str:
    if key not in config_dict or config_dict[key] in (None, ""):
        raise ValueError(f"config key '{key}' is required")
    return config_dict[key]


def load_config(path: str | pathlib.Path = "config/ghost.toml") -> ConfigStore:
    config_path = pathlib.Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    with config_path.open("rb") as f:
        data = tomli.load(f)

    config = Config(
        token=_require(data, "token"),
        db_url=_require(data, "db_url"),
        llm_model=_require(data, "llm_model"),
        reflection_model=_require(data, "reflection_model"),
        embedding_model=_require(data, "embedding_model"),
        image_model=_require(data, "image_model"),
        image_timeout_seconds=int(_require(data, "image_timeout_seconds")),
        log_level=_require(data, "log_level"),
        env=_require(data, "env"),
        max_chat_queue=int(_require(data, "max_chat_queue")),
        exclude_keywords=list(data.get("exclude_keywords", [])),
        character_prompt=data.get("character_prompt"),
        intervention_level=data.get("intervention_level"),
    )
    return ConfigStore(config)


_config_store: ConfigStore | None = None


def get_config_store() -> ConfigStore:
    global _config_store
    if _config_store is None:
        _config_store = load_config()
    return _config_store


def get_token() -> str:
    return get_config_store().config.token
