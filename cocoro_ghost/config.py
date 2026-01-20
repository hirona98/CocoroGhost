"""
設定読み込みとランタイム設定ストア

TOML設定ファイルの読み込みと、実行時に使用する統合設定の管理を行う。
設定は起動時に読み込まれ、RuntimeConfigとして各モジュールから参照される。
"""

from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

import json
import tomli

from cocoro_ghost import paths

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
    """
    TOML起動設定（起動時のみ使用、変更不可）。
    起動時に固定されるログや認証の設定を保持する。
    """
    cocoro_ghost_port: int  # CocoroGhost API の待受ポート
    token: str           # API認証用トークン
    web_auto_login_enabled: bool  # Web UI を自動ログインにする（Cookieセッションを自動発行）
    log_level: str       # ログレベル（DEBUG, INFO, WARNING, ERROR）
    llm_log_level: str   # LLM送受信ログレベル（DEBUG, INFO, OFF）
    log_file_enabled: bool  # ファイルログ有効/無効
    log_file_path: str      # ファイルログの保存先パス
    log_file_max_bytes: int  # ファイルログのローテーションサイズ（bytes）
    llm_log_console_max_chars: int  # LLM送受信ログの最大文字数（ターミナル）
    llm_log_file_max_chars: int     # LLM送受信ログの最大文字数（ファイル）
    llm_log_console_value_max_chars: int  # LLM送受信ログのValue最大文字数（ターミナル, JSON向け）
    llm_log_file_value_max_chars: int     # LLM送受信ログのValue最大文字数（ファイル, JSON向け）


@dataclass
class RuntimeConfig:
    """
    ランタイム設定（TOML + GlobalSettings + presets）。
    アプリケーション実行中に参照されるすべての設定を統合して保持する。
    """
    # TOML由来（変更不可）
    token: str       # API認証トークン
    log_level: str   # ログレベル
    web_auto_login_enabled: bool  # Web UI を自動ログインにする（Cookieセッションを自動発行）

    # GlobalSettings由来（DB設定）
    memory_enabled: bool          # 記憶機能の有効/無効
    shared_conversation_id: str   # 端末跨ぎ会話の固定ID

    # 視覚（Vision）: デスクトップウォッチ
    desktop_watch_enabled: bool
    desktop_watch_interval_seconds: int
    desktop_watch_target_client_id: Optional[str]

    # LlmPreset由来（LLM設定）
    llm_preset_name: str          # LLMプリセット名
    llm_api_key: str              # LLM APIキー
    llm_model: str                # 使用するLLMモデル
    llm_base_url: Optional[str]   # カスタムAPIエンドポイント
    reasoning_effort: Optional[str]  # 推論の詳細度設定
    max_turns_window: int         # 会話履歴の最大ターン数
    max_tokens_vision: int        # 画像認識時の最大トークン数
    max_tokens: int               # 通常時の最大トークン数
    image_model: str              # 画像認識用モデル
    image_model_api_key: Optional[str]  # 画像モデル用APIキー
    image_llm_base_url: Optional[str]   # 画像モデル用エンドポイント
    image_timeout_seconds: int    # 画像処理のタイムアウト秒数

    # EmbeddingPreset由来（埋め込みベクトル設定）
    embedding_preset_name: str    # Embeddingプリセット名
    embedding_preset_id: str      # 記憶DBのID（= EmbeddingPreset.id）
    embedding_model: str          # 埋め込みモデル名
    embedding_api_key: Optional[str]    # Embedding APIキー
    embedding_base_url: Optional[str]   # Embedding APIエンドポイント
    embedding_dimension: int      # ベクトルの次元数
    similar_episodes_limit: int   # 類似エピソード検索の上限
    max_inject_tokens: int        # プロンプトに注入する最大トークン数
    similar_limit_by_kind: Dict[str, int]  # 種別ごとの類似検索上限

    # PromptPresets由来（ユーザー編集対象）
    persona_preset_name: str      # ペルソナプリセット名
    persona_text: str             # ペルソナ定義テキスト
    second_person_label: str      # 二人称の呼称（例: マスター / あなた）
    addon_preset_name: str        # アドオンプリセット名
    addon_text: str               # アドオンテキスト


class ConfigStore:
    """
    ランタイム設定ストア（ORMを保持しない）。
    スレッドセーフに設定を管理し、各モジュールから参照可能にする。
    """

    def __init__(
        self,
        toml_config: Config,
        runtime_config: RuntimeConfig,
    ) -> None:
        self._toml = toml_config
        self._runtime = runtime_config
        # NOTE: DBセッションに紐づくORMインスタンス（GlobalSettings等）は保持しない。
        # Settings更新後も安全に参照できるよう、必要な値は RuntimeConfig にコピーして使う。
        self._lock = threading.Lock()

    @property
    def config(self) -> RuntimeConfig:
        """現在のRuntimeConfig（LLM/Embedding/Prompt等の統合設定）を返す。"""
        return self._runtime

    @property
    def toml_config(self) -> Config:
        """起動時に読み込んだTOML設定を返す。"""
        return self._toml

    @property
    def embedding_preset_id(self) -> str:
        """アクティブなEmbeddingPresetのID（= 記憶DBファイルを選ぶためのID）。"""
        return self._runtime.embedding_preset_id

    @property
    def embedding_dimension(self) -> int:
        """ベクトルDBの埋め込み次元数（embedding preset由来）。"""
        return self._runtime.embedding_dimension

    @property
    def memory_enabled(self) -> bool:
        """記憶機能の有効/無効を返す。"""
        return bool(self._runtime.memory_enabled)


def _require(config_dict: dict, key: str) -> str:
    """
    設定辞書から必須キーを取得する。
    キーが存在しないか空の場合はValueErrorを発生させる。
    """
    if key not in config_dict or config_dict[key] in (None, ""):
        raise ValueError(f"config key '{key}' is required")
    return config_dict[key]


def load_config(path: str | pathlib.Path | None = None) -> Config:
    """
    TOML設定ファイルを読み込む。
    許可されていないキーが含まれる場合はエラーを発生させる。
    """
    # --- 設定ファイルは exe の隣（config/setting.toml）を既定にする ---
    config_path = pathlib.Path(paths.get_default_config_file_path() if path is None else path)
    config_path = paths.resolve_path_under_app_root(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    # TOMLファイルをパース
    with config_path.open("rb") as f:
        data = tomli.load(f)

    # 許可されたキーのみを受け付ける
    allowed_keys = {
        "cocoro_ghost_port",
        "token",
        "web_auto_login_enabled",
        "log_level",
        "llm_log_level",
        "log_file_enabled",
        "log_file_path",
        "log_file_max_bytes",
        "llm_log_console_max_chars",
        "llm_log_file_max_chars",
        "llm_log_console_value_max_chars",
        "llm_log_file_value_max_chars",
    }
    unknown_keys = sorted(set(data.keys()) - allowed_keys)
    if unknown_keys:
        keys = ", ".join(repr(k) for k in unknown_keys)
        raise ValueError(f"unknown config key(s): {keys} (allowed: {allowed_keys})")

    # Configオブジェクトを構築
    # --- ログファイルパスは相対指定なら app_root 基準に解決する ---
    raw_log_file_path = str(data.get("log_file_path", str(paths.get_logs_dir() / "cocoro_ghost.log")))
    resolved_log_file_path = str(paths.resolve_path_under_app_root(raw_log_file_path))

    config = Config(
        # --- サーバー待受ポート（必須） ---
        cocoro_ghost_port=int(_require(data, "cocoro_ghost_port")),
        token=_require(data, "token"),
        # --- Web UI の自動ログイン（既定: 無効） ---
        web_auto_login_enabled=bool(data.get("web_auto_login_enabled", False)),
        log_level=_require(data, "log_level"),
        llm_log_level=data.get("llm_log_level", "INFO"),
        log_file_enabled=bool(data.get("log_file_enabled", False)),
        log_file_path=resolved_log_file_path,
        log_file_max_bytes=int(data.get("log_file_max_bytes", 200_000)),
        llm_log_console_max_chars=int(data.get("llm_log_console_max_chars", 2000)),
        llm_log_file_max_chars=int(data.get("llm_log_file_max_chars", 8000)),
        llm_log_console_value_max_chars=int(data.get("llm_log_console_value_max_chars", 100)),
        llm_log_file_value_max_chars=int(data.get("llm_log_file_value_max_chars", 6000)),
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
    """
    TOML、GlobalSettings、各種プリセットをマージしてRuntimeConfigを構築する。
    各設定ソースから必要な値を抽出し、統合された設定オブジェクトを返す。
    """
    # 種別ごとの類似検索上限をJSONからパース
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
        web_auto_login_enabled=bool(toml_config.web_auto_login_enabled),
        # GlobalSettings由来
        memory_enabled=bool(getattr(global_settings, "memory_enabled", True)),
        shared_conversation_id=str(getattr(global_settings, "shared_conversation_id", "") or "").strip(),
        # 視覚（Vision）: デスクトップウォッチ
        desktop_watch_enabled=bool(global_settings.desktop_watch_enabled),
        desktop_watch_interval_seconds=int(global_settings.desktop_watch_interval_seconds),
        desktop_watch_target_client_id=(
            str(global_settings.desktop_watch_target_client_id).strip()
            if global_settings.desktop_watch_target_client_id is not None
            else None
        ),
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
        embedding_preset_id=str(embedding_preset.id),
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
        second_person_label=persona_preset.second_person_label,
        addon_preset_name=addon_preset.name,
        addon_text=addon_preset.addon_text,
    )


# グローバル設定ストア（シングルトン）
_config_store: ConfigStore | None = None


def set_global_config_store(store: ConfigStore) -> None:
    """グローバルConfigStoreを設定。起動時に一度だけ呼び出される。"""
    global _config_store
    _config_store = store


def get_config_store() -> ConfigStore:
    """
    グローバルConfigStoreを取得。
    初期化されていない場合はRuntimeErrorを発生させる。
    """
    global _config_store
    if _config_store is None:
        raise RuntimeError("ConfigStore not initialized")
    return _config_store


def get_token() -> str:
    """API認証用トークンを返す。"""
    return get_config_store().config.token
