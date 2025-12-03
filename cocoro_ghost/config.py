"""設定読み込みとランタイム設定ストア。"""

from __future__ import annotations

import json
import pathlib
import threading
from dataclasses import dataclass, field
from typing import List, Optional

import tomli


@dataclass
class Config:
    """TOML起動設定（起動時のみ使用、変更不可）"""
    token: str
    db_url: str
    log_level: str
    env: str
    llm_api_key: str
    llm_model: str
    reflection_model: str
    embedding_model: str
    embedding_dimension: int
    image_model: str
    image_timeout_seconds: int
    max_chat_queue: int
    exclude_keywords: List[str] = field(default_factory=list)
    character_prompt: Optional[str] = None
    intervention_level: Optional[str] = None
    similar_episodes_limit: int = 5


@dataclass
class RuntimeConfig:
    """ランタイム設定（TOML + DB）"""
    token: str
    db_url: str
    log_level: str
    env: str
    preset_name: str
    llm_api_key: str
    llm_model: str
    reflection_model: str
    embedding_model: str
    embedding_dimension: int
    image_model: str
    image_timeout_seconds: int
    character_prompt: Optional[str]
    intervention_level: Optional[str]
    exclude_keywords: List[str]
    similar_episodes_limit: int
    max_chat_queue: int


class ConfigStore:
    """ランタイム設定ストア。"""

    def __init__(self, toml_config: Config, runtime_config: RuntimeConfig) -> None:
        self._toml = toml_config
        self._runtime = runtime_config
        self._lock = threading.Lock()

    @property
    def config(self) -> RuntimeConfig:
        return self._runtime


def _require(config_dict: dict, key: str) -> str:
    if key not in config_dict or config_dict[key] in (None, ""):
        raise ValueError(f"config key '{key}' is required")
    return config_dict[key]


def load_config(path: str | pathlib.Path = "config/setting.toml") -> Config:
    """TOML設定のみ読み込み"""
    config_path = pathlib.Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    with config_path.open("rb") as f:
        data = tomli.load(f)

    config = Config(
        token=_require(data, "token"),
        db_url=_require(data, "db_url"),
        log_level=_require(data, "log_level"),
        env=_require(data, "env"),
        llm_api_key=data.get("llm_api_key", ""),
        llm_model=data.get("llm_model", ""),
        reflection_model=data.get("reflection_model", ""),
        embedding_model=data.get("embedding_model", ""),
        embedding_dimension=int(data.get("embedding_dimension", 1536)),
        image_model=data.get("image_model", ""),
        image_timeout_seconds=int(data.get("image_timeout_seconds", 60)),
        max_chat_queue=int(data.get("max_chat_queue", 10)),
        exclude_keywords=list(data.get("exclude_keywords", [])),
        character_prompt=data.get("character_prompt"),
        intervention_level=data.get("intervention_level"),
        similar_episodes_limit=int(data.get("similar_episodes_limit", 5)),
    )
    return config


def merge_toml_and_preset(toml_config: Config, preset) -> RuntimeConfig:
    """TOMLとDBプリセットをマージ"""
    return RuntimeConfig(
        token=toml_config.token,
        db_url=toml_config.db_url,
        log_level=toml_config.log_level,
        env=toml_config.env,
        preset_name=preset.name,
        llm_api_key=preset.llm_api_key,
        llm_model=preset.llm_model,
        reflection_model=preset.reflection_model,
        embedding_model=preset.embedding_model,
        embedding_dimension=preset.embedding_dimension,
        image_model=preset.image_model,
        image_timeout_seconds=preset.image_timeout_seconds,
        character_prompt=preset.character_prompt,
        intervention_level=preset.intervention_level,
        exclude_keywords=json.loads(preset.exclude_keywords),
        similar_episodes_limit=preset.similar_episodes_limit,
        max_chat_queue=preset.max_chat_queue,
    )


_config_store: ConfigStore | None = None


def set_global_config_store(store: ConfigStore) -> None:
    """グローバルConfigStoreを設定"""
    global _config_store
    _config_store = store


def get_config_store() -> ConfigStore:
    """グローバルConfigStoreを取得"""
    global _config_store
    if _config_store is None:
        raise RuntimeError("ConfigStore not initialized")
    return _config_store


def get_token() -> str:
    return get_config_store().config.token
