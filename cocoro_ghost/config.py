"""設定読み込みとランタイム設定ストア。"""

from __future__ import annotations

import json
import pathlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

import tomli

if TYPE_CHECKING:
    from cocoro_ghost.models import (
        AddonPreset,
        EmbeddingPreset,
        GlobalSettings,
        LlmPreset,
        PersonaPreset,
    )


@dataclass
class Config:
    """TOML起動設定（起動時のみ使用、変更不可）。"""

    token: str
    log_level: str


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

    # PromptPresets由来（ユーザー編集対象）
    persona_preset_name: str
    persona_text: str
    addon_preset_name: str
    addon_text: str


class ConfigStore:
    """ランタイム設定ストア。"""

    def __init__(
        self,
        toml_config: Config,
        runtime_config: RuntimeConfig,
        global_settings: "GlobalSettings",
        llm_preset: "LlmPreset",
        embedding_preset: "EmbeddingPreset",
        persona_preset: "PersonaPreset",
        addon_preset: "AddonPreset",
    ) -> None:
        self._toml = toml_config
        self._runtime = runtime_config
        self._global_settings = global_settings
        self._llm_preset = llm_preset
        self._embedding_preset = embedding_preset
        self._persona_preset = persona_preset
        self._addon_preset = addon_preset
        self._lock = threading.Lock()

    @property
    def config(self) -> RuntimeConfig:
        """現在のRuntimeConfig（LLM/Embedding/Prompt等の統合設定）を返す。"""
        return self._runtime

    @property
    def toml_config(self) -> Config:
        """起動時に読み込んだTOML設定（token/log_level）を返す。"""
        return self._toml

    @property
    def memory_id(self) -> str:
        """アクティブな記憶DBのID（embedding preset由来）。"""
        return self._runtime.memory_id

    @property
    def embedding_dimension(self) -> int:
        """ベクトルDBの埋め込み次元数（embedding preset由来）。"""
        return self._runtime.embedding_dimension

    @property
    def memory_enabled(self) -> bool:
        """記憶機能の有効/無効を返す。"""
        return bool(getattr(self._global_settings, "memory_enabled", True))


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

    allowed_keys = {"token", "log_level"}
    unknown_keys = sorted(set(data.keys()) - allowed_keys)
    if unknown_keys:
        keys = ", ".join(repr(k) for k in unknown_keys)
        raise ValueError(f"unknown config key(s): {keys} (allowed: 'token', 'log_level')")

    config = Config(
        token=_require(data, "token"),
        log_level=_require(data, "log_level"),
    )
    return config


def build_runtime_config(
    toml_config: Config,
    global_settings: "GlobalSettings",
    llm_preset: "LlmPreset",
    embedding_preset: "EmbeddingPreset",
    persona_preset: "PersonaPreset",
    addon_preset: "AddonPreset",
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
        memory_id=str(embedding_preset.id),
        embedding_model=embedding_preset.embedding_model,
        embedding_api_key=embedding_preset.embedding_api_key,
        embedding_base_url=embedding_preset.embedding_base_url,
        embedding_dimension=embedding_preset.embedding_dimension,
        similar_episodes_limit=embedding_preset.similar_episodes_limit,
        max_inject_tokens=embedding_preset.max_inject_tokens,
        similar_limit_by_kind=similar_limit_by_kind,
        # PromptPresets由来
        persona_preset_name=persona_preset.name,
        persona_text=persona_preset.persona_text,
        addon_preset_name=addon_preset.name,
        addon_text=addon_preset.addon_text,
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
    """API認証用トークンを返す。"""
    return get_config_store().config.token
