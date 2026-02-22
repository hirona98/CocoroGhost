"""
設定読み込みとランタイム設定ストア

TOML設定ファイルの読み込みと、実行時に使用する統合設定の管理を行う。
設定は起動時に読み込まれ、RuntimeConfigとして各モジュールから参照される。
"""

from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

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
    llm_timeout_seconds: int  # LLM API（非ストリーム）のタイムアウト秒数
    llm_stream_timeout_seconds: int  # LLM API（ストリーム開始）のタイムアウト秒数
    image_summary_max_chars: int  # 画像要約（chat/notification/meta-request）: 1枚あたりの最大文字数
    retrieval_max_candidates: int  # 記憶検索: 候補収集の最大件数（SearchResultPack選別入力の上限）
    retrieval_explore_global_vector_percent: int  # 記憶検索: 探索枠（全期間ベクトル候補）の割合（retrieval_max_candidatesのX%）
    retrieval_event_affect_max_percent: int  # 記憶検索: event_affect 候補の最大割合（候補を食い過ぎない上限）
    retrieval_quota_assoc_state_percent: int  # 記憶検索: associative_recent の state 割合
    retrieval_quota_assoc_multi_hit_percent: int  # 記憶検索: associative_recent の multi-hit 割合
    retrieval_quota_assoc_vector_only_percent: int  # 記憶検索: associative_recent の vector-only 割合
    retrieval_quota_assoc_context_only_percent: int  # 記憶検索: associative_recent の context-only 割合
    retrieval_quota_assoc_entity_only_percent: int  # 記憶検索: associative_recent の entity-only 割合（entity_expand単独hit）
    retrieval_quota_assoc_trigram_only_percent: int  # 記憶検索: associative_recent の trigram-only 割合
    retrieval_quota_assoc_recent_only_percent: int  # 記憶検索: associative_recent の recent-only 割合
    retrieval_quota_broad_state_percent: int  # 記憶検索: targeted_broad/explicit_about_time の state 割合
    retrieval_quota_broad_multi_hit_percent: int  # 記憶検索: targeted_broad/explicit_about_time の multi-hit 割合
    retrieval_quota_broad_about_time_only_percent: int  # 記憶検索: targeted_broad/explicit_about_time の about_time-only 割合
    retrieval_quota_broad_vector_only_percent: int  # 記憶検索: targeted_broad/explicit_about_time の vector-only 割合
    retrieval_quota_broad_context_only_percent: int  # 記憶検索: targeted_broad/explicit_about_time の context-only 割合
    retrieval_quota_broad_entity_only_percent: int  # 記憶検索: targeted_broad/explicit_about_time の entity-only 割合（entity_expand単独hit）
    retrieval_quota_broad_trigram_only_percent: int  # 記憶検索: targeted_broad/explicit_about_time の trigram-only 割合
    retrieval_entity_expand_enabled: bool  # 記憶検索: entity展開（seed→entity→関連候補）を有効化
    retrieval_entity_expand_seed_limit: int  # 記憶検索: entity展開のseed上限
    retrieval_entity_expand_max_entities: int  # 記憶検索: 展開に使うentity上限
    retrieval_entity_expand_per_entity_event_limit: int  # 記憶検索: entityごとの関連event上限
    retrieval_entity_expand_per_entity_state_limit: int  # 記憶検索: entityごとの関連state上限
    retrieval_entity_expand_min_confidence: float  # 記憶検索: entity展開のconfidence足切り（0.0..1.0）
    retrieval_entity_expand_min_seed_occurrences: int  # 記憶検索: entity展開のseed内出現回数の足切り
    retrieval_state_link_expand_enabled: bool  # 記憶検索: stateリンク展開（seed→state_links→関連state）を有効化
    retrieval_state_link_expand_seed_limit: int  # 記憶検索: stateリンク展開のseed上限
    retrieval_state_link_expand_per_seed_limit: int  # 記憶検索: seedごとの展開上限
    retrieval_state_link_expand_min_confidence: float  # 記憶検索: stateリンク展開のconfidence足切り（0.0..1.0）
    memory_state_links_build_enabled: bool  # 非同期: state_links を生成する
    memory_state_links_build_target_state_limit: int  # 非同期: 1回のWritePlanで処理するstate上限
    memory_state_links_build_candidate_k: int  # 非同期: ベクトル近傍の候補数（k）
    memory_state_links_build_max_links_per_state: int  # 非同期: 1stateあたりのリンク上限
    memory_state_links_build_min_confidence: float  # 非同期: リンク採用のconfidence足切り（0.0..1.0）
    memory_state_links_build_max_distance: float  # 非同期: ベクトル距離の足切り（小さいほど近い）
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
    llm_timeout_seconds: int  # LLM API（非ストリーム）のタイムアウト秒数
    llm_stream_timeout_seconds: int  # LLM API（ストリーム開始）のタイムアウト秒数

    # GlobalSettings由来（DB設定）
    memory_enabled: bool          # 記憶機能（常時有効）
    shared_conversation_id: str   # 端末跨ぎ会話の固定ID

    # 視覚（Vision）: デスクトップウォッチ
    desktop_watch_enabled: bool
    desktop_watch_interval_seconds: int
    desktop_watch_target_client_id: Optional[str]

    # 自発行動（Autonomy）
    autonomy_enabled: bool
    autonomy_heartbeat_seconds: int
    autonomy_max_parallel_intents: int

    # 視覚（Vision）: カメラ監視
    camera_watch_enabled: bool
    camera_watch_interval_seconds: int

    # LlmPreset由来（LLM設定）
    llm_preset_name: str          # LLMプリセット名
    llm_api_key: str              # LLM APIキー
    llm_model: str                # 使用するLLMモデル
    llm_base_url: Optional[str]   # カスタムAPIエンドポイント
    reasoning_effort: Optional[str]  # 推論の詳細度設定
    reply_web_search_enabled: bool  # 最終応答（SYNC_CONVERSATION）でWeb検索を有効化するか
    deliberation_model: str       # Deliberation専用モデル
    deliberation_max_tokens: int  # Deliberationの最大トークン
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
        """記憶機能の有効状態を返す（常に true）。"""
        return bool(self._runtime.memory_enabled)


def _require(config_dict: dict, key: str) -> Any:
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
        "llm_timeout_seconds",
        "llm_stream_timeout_seconds",
        "image_summary_max_chars",
        "retrieval_max_candidates",
        "retrieval_explore_global_vector_percent",
        "retrieval_event_affect_max_percent",
        "retrieval_quota_assoc_state_percent",
        "retrieval_quota_assoc_multi_hit_percent",
        "retrieval_quota_assoc_vector_only_percent",
        "retrieval_quota_assoc_context_only_percent",
        "retrieval_quota_assoc_entity_only_percent",
        "retrieval_quota_assoc_trigram_only_percent",
        "retrieval_quota_assoc_recent_only_percent",
        "retrieval_quota_broad_state_percent",
        "retrieval_quota_broad_multi_hit_percent",
        "retrieval_quota_broad_about_time_only_percent",
        "retrieval_quota_broad_vector_only_percent",
        "retrieval_quota_broad_context_only_percent",
        "retrieval_quota_broad_entity_only_percent",
        "retrieval_quota_broad_trigram_only_percent",
        "retrieval_entity_expand_enabled",
        "retrieval_entity_expand_seed_limit",
        "retrieval_entity_expand_max_entities",
        "retrieval_entity_expand_per_entity_event_limit",
        "retrieval_entity_expand_per_entity_state_limit",
        "retrieval_entity_expand_min_confidence",
        "retrieval_entity_expand_min_seed_occurrences",
        "retrieval_state_link_expand_enabled",
        "retrieval_state_link_expand_seed_limit",
        "retrieval_state_link_expand_per_seed_limit",
        "retrieval_state_link_expand_min_confidence",
        "memory_state_links_build_enabled",
        "memory_state_links_build_target_state_limit",
        "memory_state_links_build_candidate_k",
        "memory_state_links_build_max_links_per_state",
        "memory_state_links_build_min_confidence",
        "memory_state_links_build_max_distance",
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

    # --- LLMタイムアウト（必須・正の整数） ---
    # NOTE: 0以下だと実質的に「無限待ち」になり得るため、起動時に弾く。
    llm_timeout_seconds = int(_require(data, "llm_timeout_seconds"))
    llm_stream_timeout_seconds = int(_require(data, "llm_stream_timeout_seconds"))
    if llm_timeout_seconds <= 0:
        raise ValueError("llm_timeout_seconds must be a positive integer")
    if llm_stream_timeout_seconds <= 0:
        raise ValueError("llm_stream_timeout_seconds must be a positive integer")

    # --- 画像要約: 1枚あたり最大文字数（必須・正の整数） ---
    # NOTE:
    # - chat/notification/meta-request の data URI 画像から要約を作るときに使う。
    # - 長すぎる要約は後段のプロンプトを膨らませ、体感と品質を悪化させやすい。
    image_summary_max_chars = int(_require(data, "image_summary_max_chars"))
    if image_summary_max_chars <= 0:
        raise ValueError("image_summary_max_chars must be a positive integer")

    # --- 記憶検索: 候補数上限（既定: 60） ---
    # NOTE:
    # - SearchResultPack の「候補収集 → LLM選別」は SSE 開始前の同期経路なので、候補が膨らむと体感が悪化する。
    # - ここは品質を維持しつつ体感を改善するための「上限」で、実装側で必ず強制する。
    retrieval_max_candidates = int(data.get("retrieval_max_candidates", 60))
    if retrieval_max_candidates <= 0:
        raise ValueError("retrieval_max_candidates must be a positive integer")

    # --- 記憶検索: 割合配分（既定は品質優先の推奨値） ---
    # NOTE:
    # - 目的: 特定経路（trigram/context/vectorなど）が大量に出たときに、候補が偏って選別入力が膨らむのを抑える。
    # - multi-hit は「複数経路で当たった候補」で、品質（取りこぼし防止）に効くので厚めにする。
    # - state は会話の安定性に効くため、一定割合を確保する。
    # - event_affect は候補を食い過ぎやすいので「最大割合」のみ設ける（最小件数は強制しない）。
    #
    # 設計方針（重要）:
    # - 「割合の合計が100%」である必要はない（100%未満でもOK）。
    #   足りない分は実装側で埋めるため、細かい調整がしやすい。
    # - 100%を超える設定は入力の意味が崩れるため起動時に弾く。
    def require_percent(key: str, default: int) -> int:
        v = int(data.get(key, default))
        if v < 0 or v > 100:
            raise ValueError(f"{key} must be in 0..100")
        return int(v)

    # --- 探索枠（全期間ベクトル候補）の割合 ---
    # NOTE:
    # - 「ひらめき」を得るため、ユーザーが期間指定しなくても、毎ターン少量だけ“全期間”の候補を混ぜる。
    # - ここは retrieval_max_candidates（候補数）に対する割合（X%）で指定する。
    # - 0% にすると探索枠は無効になる（= 従来の確度重視だけになる）。
    retrieval_explore_global_vector_percent = require_percent("retrieval_explore_global_vector_percent", 5)

    # --- event_affect の最大割合（候補の上限キャップ） ---
    # NOTE:
    # - event_affect は会話のトーン調整には効くが、候補が増えると state/event の枠を圧迫しやすい。
    # - ここは「最低件数を保証」せず「最大割合だけ」を設定する（不足時に無理に混ぜてノイズを増やさない）。
    retrieval_event_affect_max_percent = require_percent("retrieval_event_affect_max_percent", 5)

    # associative_recent
    retrieval_quota_assoc_state_percent = require_percent("retrieval_quota_assoc_state_percent", 20)
    retrieval_quota_assoc_multi_hit_percent = require_percent("retrieval_quota_assoc_multi_hit_percent", 40)
    # NOTE: vector_only は「最近寄り」の意味近傍。探索枠（全期間）とは別に扱う。
    retrieval_quota_assoc_vector_only_percent = require_percent("retrieval_quota_assoc_vector_only_percent", 15)
    retrieval_quota_assoc_context_only_percent = require_percent("retrieval_quota_assoc_context_only_percent", 10)
    retrieval_quota_assoc_entity_only_percent = require_percent("retrieval_quota_assoc_entity_only_percent", 5)
    retrieval_quota_assoc_trigram_only_percent = require_percent("retrieval_quota_assoc_trigram_only_percent", 5)
    retrieval_quota_assoc_recent_only_percent = require_percent("retrieval_quota_assoc_recent_only_percent", 0)
    assoc_sum = (
        int(retrieval_quota_assoc_state_percent)
        + int(retrieval_quota_assoc_multi_hit_percent)
        + int(retrieval_explore_global_vector_percent)
        + int(retrieval_quota_assoc_vector_only_percent)
        + int(retrieval_quota_assoc_context_only_percent)
        + int(retrieval_quota_assoc_entity_only_percent)
        + int(retrieval_quota_assoc_trigram_only_percent)
        + int(retrieval_quota_assoc_recent_only_percent)
    )
    if assoc_sum > 100:
        raise ValueError("retrieval_quota_assoc_*_percent sum must be <= 100")

    # targeted_broad / explicit_about_time
    retrieval_quota_broad_state_percent = require_percent("retrieval_quota_broad_state_percent", 20)
    retrieval_quota_broad_multi_hit_percent = require_percent("retrieval_quota_broad_multi_hit_percent", 30)
    retrieval_quota_broad_about_time_only_percent = require_percent("retrieval_quota_broad_about_time_only_percent", 20)
    # NOTE: vector_only は「最近寄り」の意味近傍。探索枠（全期間）とは別に扱う。
    retrieval_quota_broad_vector_only_percent = require_percent("retrieval_quota_broad_vector_only_percent", 10)
    retrieval_quota_broad_context_only_percent = require_percent("retrieval_quota_broad_context_only_percent", 5)
    retrieval_quota_broad_entity_only_percent = require_percent("retrieval_quota_broad_entity_only_percent", 5)
    retrieval_quota_broad_trigram_only_percent = require_percent("retrieval_quota_broad_trigram_only_percent", 5)
    broad_sum = (
        int(retrieval_quota_broad_state_percent)
        + int(retrieval_quota_broad_multi_hit_percent)
        + int(retrieval_quota_broad_about_time_only_percent)
        + int(retrieval_explore_global_vector_percent)
        + int(retrieval_quota_broad_vector_only_percent)
        + int(retrieval_quota_broad_context_only_percent)
        + int(retrieval_quota_broad_entity_only_percent)
        + int(retrieval_quota_broad_trigram_only_percent)
    )
    if broad_sum > 100:
        raise ValueError("retrieval_quota_broad_*_percent sum must be <= 100")

    # --- 記憶検索: entity展開（seed→entity→関連候補） ---
    # NOTE:
    # - entity索引（event_entities/state_entities）を使って「多段想起」を安定化する。
    # - 候補が肥大化しやすいので、上限/足切りをTOMLで固定する。
    retrieval_entity_expand_enabled = bool(data.get("retrieval_entity_expand_enabled", True))
    retrieval_entity_expand_seed_limit = int(data.get("retrieval_entity_expand_seed_limit", 12))
    retrieval_entity_expand_max_entities = int(data.get("retrieval_entity_expand_max_entities", 12))
    retrieval_entity_expand_per_entity_event_limit = int(data.get("retrieval_entity_expand_per_entity_event_limit", 10))
    retrieval_entity_expand_per_entity_state_limit = int(data.get("retrieval_entity_expand_per_entity_state_limit", 6))
    try:
        retrieval_entity_expand_min_confidence = float(data.get("retrieval_entity_expand_min_confidence", 0.45))
    except Exception:  # noqa: BLE001
        retrieval_entity_expand_min_confidence = 0.45
    retrieval_entity_expand_min_seed_occurrences = int(data.get("retrieval_entity_expand_min_seed_occurrences", 2))

    # --- 記憶検索: stateリンク展開（seed→state_links→関連state） ---
    retrieval_state_link_expand_enabled = bool(data.get("retrieval_state_link_expand_enabled", True))
    retrieval_state_link_expand_seed_limit = int(data.get("retrieval_state_link_expand_seed_limit", 8))
    retrieval_state_link_expand_per_seed_limit = int(data.get("retrieval_state_link_expand_per_seed_limit", 8))
    try:
        retrieval_state_link_expand_min_confidence = float(data.get("retrieval_state_link_expand_min_confidence", 0.6))
    except Exception:  # noqa: BLE001
        retrieval_state_link_expand_min_confidence = 0.6

    # --- 非同期: state_links 生成（B-1） ---
    memory_state_links_build_enabled = bool(data.get("memory_state_links_build_enabled", True))
    memory_state_links_build_target_state_limit = int(data.get("memory_state_links_build_target_state_limit", 3))
    memory_state_links_build_candidate_k = int(data.get("memory_state_links_build_candidate_k", 24))
    memory_state_links_build_max_links_per_state = int(data.get("memory_state_links_build_max_links_per_state", 6))
    try:
        memory_state_links_build_min_confidence = float(data.get("memory_state_links_build_min_confidence", 0.65))
    except Exception:  # noqa: BLE001
        memory_state_links_build_min_confidence = 0.65
    try:
        memory_state_links_build_max_distance = float(data.get("memory_state_links_build_max_distance", 0.35))
    except Exception:  # noqa: BLE001
        memory_state_links_build_max_distance = 0.35

    config = Config(
        # --- サーバー待受ポート（必須） ---
        cocoro_ghost_port=int(_require(data, "cocoro_ghost_port")),
        token=_require(data, "token"),
        # --- Web UI の自動ログイン（既定: 無効） ---
        web_auto_login_enabled=bool(data.get("web_auto_login_enabled", False)),
        log_level=_require(data, "log_level"),
        llm_log_level=data.get("llm_log_level", "INFO"),
        # --- LLM タイムアウト（必須） ---
        # NOTE:
        # - 外部LLMがハングするとAPI応答も返らなくなるため、明示的に上限を設ける。
        # - stream は「開始まで」の待ちを制限する目的（本文生成はストリームで継続受信する）。
        llm_timeout_seconds=int(llm_timeout_seconds),
        llm_stream_timeout_seconds=int(llm_stream_timeout_seconds),
        # --- 画像要約: 1枚あたり最大文字数（必須） ---
        image_summary_max_chars=int(image_summary_max_chars),
        # --- 記憶検索（候補上限） ---
        retrieval_max_candidates=int(retrieval_max_candidates),
        retrieval_explore_global_vector_percent=int(retrieval_explore_global_vector_percent),
        retrieval_event_affect_max_percent=int(retrieval_event_affect_max_percent),
        retrieval_quota_assoc_state_percent=int(retrieval_quota_assoc_state_percent),
        retrieval_quota_assoc_multi_hit_percent=int(retrieval_quota_assoc_multi_hit_percent),
        retrieval_quota_assoc_vector_only_percent=int(retrieval_quota_assoc_vector_only_percent),
        retrieval_quota_assoc_context_only_percent=int(retrieval_quota_assoc_context_only_percent),
        retrieval_quota_assoc_entity_only_percent=int(retrieval_quota_assoc_entity_only_percent),
        retrieval_quota_assoc_trigram_only_percent=int(retrieval_quota_assoc_trigram_only_percent),
        retrieval_quota_assoc_recent_only_percent=int(retrieval_quota_assoc_recent_only_percent),
        retrieval_quota_broad_state_percent=int(retrieval_quota_broad_state_percent),
        retrieval_quota_broad_multi_hit_percent=int(retrieval_quota_broad_multi_hit_percent),
        retrieval_quota_broad_about_time_only_percent=int(retrieval_quota_broad_about_time_only_percent),
        retrieval_quota_broad_vector_only_percent=int(retrieval_quota_broad_vector_only_percent),
        retrieval_quota_broad_context_only_percent=int(retrieval_quota_broad_context_only_percent),
        retrieval_quota_broad_entity_only_percent=int(retrieval_quota_broad_entity_only_percent),
        retrieval_quota_broad_trigram_only_percent=int(retrieval_quota_broad_trigram_only_percent),
        retrieval_entity_expand_enabled=bool(retrieval_entity_expand_enabled),
        retrieval_entity_expand_seed_limit=int(retrieval_entity_expand_seed_limit),
        retrieval_entity_expand_max_entities=int(retrieval_entity_expand_max_entities),
        retrieval_entity_expand_per_entity_event_limit=int(retrieval_entity_expand_per_entity_event_limit),
        retrieval_entity_expand_per_entity_state_limit=int(retrieval_entity_expand_per_entity_state_limit),
        retrieval_entity_expand_min_confidence=float(retrieval_entity_expand_min_confidence),
        retrieval_entity_expand_min_seed_occurrences=int(retrieval_entity_expand_min_seed_occurrences),
        retrieval_state_link_expand_enabled=bool(retrieval_state_link_expand_enabled),
        retrieval_state_link_expand_seed_limit=int(retrieval_state_link_expand_seed_limit),
        retrieval_state_link_expand_per_seed_limit=int(retrieval_state_link_expand_per_seed_limit),
        retrieval_state_link_expand_min_confidence=float(retrieval_state_link_expand_min_confidence),
        memory_state_links_build_enabled=bool(memory_state_links_build_enabled),
        memory_state_links_build_target_state_limit=int(memory_state_links_build_target_state_limit),
        memory_state_links_build_candidate_k=int(memory_state_links_build_candidate_k),
        memory_state_links_build_max_links_per_state=int(memory_state_links_build_max_links_per_state),
        memory_state_links_build_min_confidence=float(memory_state_links_build_min_confidence),
        memory_state_links_build_max_distance=float(memory_state_links_build_max_distance),
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
        llm_timeout_seconds=int(toml_config.llm_timeout_seconds),
        llm_stream_timeout_seconds=int(toml_config.llm_stream_timeout_seconds),
        # GlobalSettings由来
        # 記憶機能は常時有効のため、ランタイムでは true に固定する。
        memory_enabled=True,
        shared_conversation_id=str(getattr(global_settings, "shared_conversation_id", "") or "").strip(),
        # 視覚（Vision）: デスクトップウォッチ
        desktop_watch_enabled=bool(global_settings.desktop_watch_enabled),
        desktop_watch_interval_seconds=int(global_settings.desktop_watch_interval_seconds),
        desktop_watch_target_client_id=(
            str(global_settings.desktop_watch_target_client_id).strip()
            if global_settings.desktop_watch_target_client_id is not None
            else None
        ),
        autonomy_enabled=bool(getattr(global_settings, "autonomy_enabled", False)),
        autonomy_heartbeat_seconds=max(1, int(getattr(global_settings, "autonomy_heartbeat_seconds", 30))),
        autonomy_max_parallel_intents=max(1, int(getattr(global_settings, "autonomy_max_parallel_intents", 2))),
        camera_watch_enabled=bool(getattr(global_settings, "camera_watch_enabled", False)),
        camera_watch_interval_seconds=max(1, int(getattr(global_settings, "camera_watch_interval_seconds", 15))),
        # LlmPreset由来
        llm_preset_name=llm_preset.name,
        llm_api_key=llm_preset.llm_api_key,
        llm_model=llm_preset.llm_model,
        llm_base_url=llm_preset.llm_base_url,
        reasoning_effort=llm_preset.reasoning_effort,
        reply_web_search_enabled=bool(llm_preset.reply_web_search_enabled),
        deliberation_model=str(getattr(llm_preset, "deliberation_model", llm_preset.llm_model)),
        deliberation_max_tokens=max(1, int(getattr(llm_preset, "deliberation_max_tokens", 1200))),
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
