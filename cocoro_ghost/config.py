"""設定読み込みとランタイム設定ストア。"""

from __future__ import annotations

import json
import pathlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import tomli

if TYPE_CHECKING:
    from cocoro_ghost.models import (
        ContractPreset,
        EmbeddingPreset,
        GlobalSettings,
        LlmPreset,
        PersonaPreset,
        SystemPromptPreset,
    )


@dataclass
class Config:
    """TOML起動設定（起動時のみ使用、変更不可）。"""

    token: str
    log_level: str
    llm_api_key: str = ""
    llm_model: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 3072
    image_model: str = ""
    image_timeout_seconds: int = 60
    exclude_keywords: List[str] = field(default_factory=list)
    character_prompt: Optional[str] = None
    similar_episodes_limit: int = 5


@dataclass
class RuntimeConfig:
    """ランタイム設定（TOML + GlobalSettings + presets）。"""

    # TOML由来（変更不可）
    token: str
    log_level: str

    # GlobalSettings由来
    exclude_keywords: List[str]

    # LlmPreset由来
    llm_preset_name: str
    llm_api_key: str
    llm_model: str
    llm_base_url: Optional[str]
    reasoning_effort: Optional[str]
    max_turns_window: int
    max_tokens_vision: int
    max_tokens: int
    image_model: str
    image_model_api_key: Optional[str]
    image_llm_base_url: Optional[str]
    image_timeout_seconds: int

    # EmbeddingPreset由来
    embedding_preset_name: str
    memory_id: str
    embedding_model: str
    embedding_api_key: Optional[str]
    embedding_base_url: Optional[str]
    embedding_dimension: int
    similar_episodes_limit: int
    max_inject_tokens: int
    similar_limit_by_kind: Dict[str, int]

    # PromptPresets由来
    system_prompt_preset_name: str
    system_prompt: str
    persona_preset_name: str
    persona_text: str
    contract_preset_name: str
    contract_text: str


class ConfigStore:
    """ランタイム設定ストア。"""

    def __init__(
        self,
        toml_config: Config,
        runtime_config: RuntimeConfig,
        global_settings: "GlobalSettings",
        llm_preset: "LlmPreset",
        embedding_preset: "EmbeddingPreset",
        system_prompt_preset: "SystemPromptPreset",
        persona_preset: "PersonaPreset",
        contract_preset: "ContractPreset",
    ) -> None:
        self._toml = toml_config
        self._runtime = runtime_config
        self._global_settings = global_settings
        self._llm_preset = llm_preset
        self._embedding_preset = embedding_preset
        self._system_prompt_preset = system_prompt_preset
        self._persona_preset = persona_preset
        self._contract_preset = contract_preset
        self._lock = threading.Lock()

    @property
    def config(self) -> RuntimeConfig:
        return self._runtime

    @property
    def toml_config(self) -> Config:
        return self._toml

    @property
    def memory_id(self) -> str:
        return self._runtime.memory_id

    @property
    def embedding_dimension(self) -> int:
        return self._runtime.embedding_dimension


def _require(config_dict: dict, key: str) -> str:
    if key not in config_dict or config_dict[key] in (None, ""):
        raise ValueError(f"config key '{key}' is required")
    return config_dict[key]


def load_config(path: str | pathlib.Path = "config/setting.toml") -> Config:
    """TOML設定のみ読み込み。"""
    config_path = pathlib.Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    with config_path.open("rb") as f:
        data = tomli.load(f)

    config = Config(
        token=_require(data, "token"),
        log_level=_require(data, "log_level"),
        llm_api_key=data.get("llm_api_key", ""),
        llm_model=data.get("llm_model", ""),
        embedding_model=data.get("embedding_model", ""),
        embedding_dimension=int(data.get("embedding_dimension", 3072)),
        image_model=data.get("image_model", ""),
        image_timeout_seconds=int(data.get("image_timeout_seconds", 60)),
        exclude_keywords=list(data.get("exclude_keywords", [])),
        character_prompt=data.get("character_prompt"),
        similar_episodes_limit=int(data.get("similar_episodes_limit", 5)),
    )
    return config


def build_runtime_config(
    toml_config: Config,
    global_settings: "GlobalSettings",
    llm_preset: "LlmPreset",
    embedding_preset: "EmbeddingPreset",
    system_prompt_preset: "SystemPromptPreset",
    persona_preset: "PersonaPreset",
    contract_preset: "ContractPreset",
) -> RuntimeConfig:
    """TOML、GlobalSettings、各種プリセットをマージしてRuntimeConfigを構築。"""
    try:
        similar_limit_by_kind = json.loads(embedding_preset.similar_limit_by_kind_json or "{}")
        if not isinstance(similar_limit_by_kind, dict):
            similar_limit_by_kind = {}
    except Exception:  # noqa: BLE001
        similar_limit_by_kind = {}

    return RuntimeConfig(
        # TOML由来
        token=global_settings.token or toml_config.token,
        log_level=toml_config.log_level,
        # GlobalSettings由来
        exclude_keywords=json.loads(global_settings.exclude_keywords),
        # LlmPreset由来
        llm_preset_name=llm_preset.name,
        llm_api_key=llm_preset.llm_api_key,
        llm_model=llm_preset.llm_model,
        llm_base_url=llm_preset.llm_base_url,
        reasoning_effort=llm_preset.reasoning_effort,
        max_turns_window=llm_preset.max_turns_window,
        max_tokens_vision=llm_preset.max_tokens_vision,
        max_tokens=llm_preset.max_tokens,
        image_model=llm_preset.image_model,
        image_model_api_key=llm_preset.image_model_api_key,
        image_llm_base_url=llm_preset.image_llm_base_url,
        image_timeout_seconds=llm_preset.image_timeout_seconds,
        # EmbeddingPreset由来
        embedding_preset_name=embedding_preset.name,
        memory_id=embedding_preset.name,
        embedding_model=embedding_preset.embedding_model,
        embedding_api_key=embedding_preset.embedding_api_key,
        embedding_base_url=embedding_preset.embedding_base_url,
        embedding_dimension=embedding_preset.embedding_dimension,
        similar_episodes_limit=embedding_preset.similar_episodes_limit,
        max_inject_tokens=embedding_preset.max_inject_tokens,
        similar_limit_by_kind=similar_limit_by_kind,
        # PromptPresets由来
        system_prompt_preset_name=system_prompt_preset.name,
        system_prompt=system_prompt_preset.system_prompt,
        persona_preset_name=persona_preset.name,
        persona_text=persona_preset.persona_text,
        contract_preset_name=contract_preset.name,
        contract_text=contract_preset.contract_text,
    )


_config_store: ConfigStore | None = None


def set_global_config_store(store: ConfigStore) -> None:
    """グローバルConfigStoreを設定。"""
    global _config_store
    _config_store = store


def get_config_store() -> ConfigStore:
    """グローバルConfigStoreを取得。"""
    global _config_store
    if _config_store is None:
        raise RuntimeError("ConfigStore not initialized")
    return _config_store


def get_token() -> str:
    return get_config_store().config.token
