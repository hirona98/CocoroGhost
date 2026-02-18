"""cocoro_ghost.llm_client

LiteLLM ラッパー。

LLM API 呼び出しを抽象化するクライアントクラス。
現状は `litellm.completion()`（OpenAI の chat.completions 互換の messages 形式）を中心に利用する。
会話生成、JSON 生成、埋め込みベクトル生成、画像認識をサポートする。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Generator, Iterable, List, Optional

import litellm

from cocoro_ghost.llm_debug import log_llm_payload, normalize_llm_log_level, truncate_for_log


def _to_openai_compatible_model_for_slug(slug: str) -> str:
    """
    OpenAI互換エンドポイントへ投げるための LiteLLM model 名に変換する。

    前提:
    - LiteLLM の OpenAI 互換プロバイダは `openai/<model>` の形式を要求する。
    - このとき `<model>` はそのまま HTTP payload の `model` に入る（OpenRouter の model slug を想定）。
      例: OpenRouter に `model="google/gemini-embedding-001"` を渡したい場合、
          LiteLLM には `model="openai/google/gemini-embedding-001"` を渡す。
      例: OpenRouter に `model="openai/text-embedding-3-large"` を渡したい場合、
          LiteLLM には `model="openai/openai/text-embedding-3-large"` を渡す。
    """
    # --- 空文字は呼び出し側の責務として弾く（ここでは整形しない） ---
    cleaned = str(slug or "").strip()
    return f"openai/{cleaned}"


def _openrouter_slug_from_model(model: str) -> str:
    """
    モデル文字列から OpenRouter の model slug を取り出す。

    目的:
    - `openrouter/<slug>` のような表記でも、埋め込みでは OpenAI互換（openai/）で呼び出す必要があるため、
      `<slug>` を取り出して共通化する。
    """
    # --- 余計な空白を除去してから判定する ---
    cleaned = str(model or "").strip()
    if cleaned.startswith("openrouter/"):
        return cleaned.removeprefix("openrouter/")
    return cleaned


def _validate_openrouter_embedding_slug(slug: str) -> None:
    """OpenRouterのEmbeddingモデルslugとして妥当かを軽く検査する。

    OpenRouterではチャットモデルとEmbeddingモデルのslugが別で、
    チャットモデル（例: gemini-*-flash / *-preview）を embeddings に投げると失敗する。

    この関数は「誤設定でハマりやすいケース」だけを明示的に弾き、
    それ以外はOpenRouter側のエラーに委ねる。

    Args:
        slug: OpenRouterのmodel slug（例: google/gemini-embedding-001）。

    Raises:
        ValueError: Embeddingとして不適切なslugが推測される場合。
    """
    # --- Geminiチャット系の誤設定を狙い撃ちで弾く ---
    cleaned = str(slug or "").strip()
    if not cleaned:
        raise ValueError("embedding model slug is empty")

    is_google_gemini = cleaned.startswith("google/gemini-")
    looks_like_chat = any(key in cleaned for key in ["flash", "pro", "preview"]) and "embedding" not in cleaned
    if is_google_gemini and looks_like_chat:
        raise ValueError(
            "Embedding model seems to be a chat model slug. "
            "For OpenRouter + Gemini embeddings, set embedding_model to something like "
            "'openrouter/google/gemini-embedding-001' (not a gemini-*-flash/pro/preview chat model)."
        )


def _get_embedding_api_base(*, embedding_model: str, embedding_base_url: str | None) -> str | None:
    """
    Embedding 用の api_base を決定する。

    方針:
    - embedding_base_url が明示されていれば、それを最優先で使う（ローカルLLM等の用途）
    - embedding_model が `openrouter/` なら、OpenRouter の OpenAI 互換エンドポイントを自動設定する

    NOTE:
    - OpenRouter embeddings は `provider=openrouter` ではなく OpenAI 互換（openai/）で呼ぶ必要があるため、
      api_base も合わせて自動設定して「設定を楽にする」。
    """
    # --- 明示指定が最優先（ローカルLLM等） ---
    if embedding_base_url and str(embedding_base_url).strip():
        return str(embedding_base_url).strip()

    # --- OpenRouter の場合だけ自動設定する ---
    model_cleaned = str(embedding_model or "").strip()
    if model_cleaned.startswith("openrouter/"):
        return "https://openrouter.ai/api/v1"

    return None


def _response_to_dict(resp: Any) -> Dict[str, Any]:
    """
    LiteLLM Responseをシリアライズ可能なdictに変換する。
    デバッグやログ出力に使用する。
    """
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if hasattr(resp, "dict"):
        return resp.dict()
    if hasattr(resp, "json"):
        try:
            return json.loads(resp.json())
        except Exception:  # noqa: BLE001
            pass
    return dict(resp) if isinstance(resp, dict) else {"raw": str(resp)}


def _first_choice_content(resp: Any) -> str:
    """
    choices[0].message.contentを取り出すユーティリティ。
    LLMレスポンスから本文テキストを抽出する。
    """
    try:
        choice = resp.choices[0]
        message = getattr(choice, "message", None) or choice["message"]
        content = getattr(message, "content", None) or message["content"]
        # OpenAI形式で content が list の場合もあるため統一
        if isinstance(content, list):
            return "".join([item.get("text", "") if isinstance(item, dict) else str(item) for item in content]) or ""
        return content or ""
    except Exception:  # noqa: BLE001
        return ""


def _delta_content(resp: Any) -> str:
    """
    ストリーミングチャンクからdelta.contentを取り出す。
    ストリーミング応答の逐次処理に使用する。
    """
    try:
        choice = resp.choices[0]
        delta = getattr(choice, "delta", None) or choice.get("delta")
        if not delta:
            return ""
        content = getattr(delta, "content", None) or delta.get("content")
        if content is None:
            return ""
        # OpenAI形式で content が list の場合もあるため統一
        if isinstance(content, list):
            return "".join([item.get("text", "") if isinstance(item, dict) else str(item) for item in content])
        return str(content)
    except Exception:  # noqa: BLE001
        return ""


_DATA_IMAGE_URL_RE = re.compile(r"\bdata:image/[^;]+;base64,\S+", re.IGNORECASE)


def _mask_data_image_urls(text: str) -> str:
    """data:image/...;base64,... をログから除外する。"""
    if not text:
        return ""

    def _repl(m: re.Match) -> str:
        matched = m.group(0)
        return f"(data-image-url omitted, chars={len(matched)})"

    return _DATA_IMAGE_URL_RE.sub(_repl, text)


def _sanitize_for_llm_log(obj: Any, *, max_depth: int = 8) -> Any:
    """LLM送受信ログ向けに payload を軽くサニタイズする。"""
    if max_depth <= 0:
        return "..."

    if obj is None:
        return None

    if isinstance(obj, str):
        return _mask_data_image_urls(obj)

    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes__": True, "len": len(obj)}

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out[str(k)] = _sanitize_for_llm_log(v, max_depth=max_depth - 1)
        return out

    if isinstance(obj, list):
        return [_sanitize_for_llm_log(v, max_depth=max_depth - 1) for v in obj]

    if isinstance(obj, tuple):
        return tuple(_sanitize_for_llm_log(v, max_depth=max_depth - 1) for v in obj)

    # pydantic / dataclass 等は文字列化（ログ用途なので落とさない）
    try:
        return str(obj)
    except Exception:  # noqa: BLE001
        return "(unserializable)"


def _estimate_text_chars(obj: Any) -> int:
    """INFOログ用の「おおまかな文字量」を見積もる。"""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, (bytes, bytearray)):
        return len(obj)
    if isinstance(obj, dict):
        return sum(_estimate_text_chars(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_estimate_text_chars(v) for v in obj)
    try:
        return len(str(obj))
    except Exception:  # noqa: BLE001
        return 0


# コードフェンス検出用正規表現
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """```json ... ```のようなフェンスがあれば中身だけを取り出す。"""
    if not text:
        return ""
    m = _JSON_FENCE_RE.search(text)
    return m.group(1) if m else text


def _extract_first_json_value(text: str) -> str:
    """
    文字列から最初のJSON値（object/array）らしき部分を抜き出す。
    LLMが出力した「ほぼJSON」から有効な部分を抽出する。
    """
    text = _strip_code_fences(text).strip()
    if not text:
        return ""

    # { か [ の最初の出現位置を探す
    obj_i = text.find("{")
    arr_i = text.find("[")
    if obj_i == -1 and arr_i == -1:
        return text

    # 開始文字と閉じ文字を決定
    if obj_i == -1:
        start = arr_i
        open_ch, close_ch = "[", "]"
    elif arr_i == -1:
        start = obj_i
        open_ch, close_ch = "{", "}"
    else:
        start = obj_i if obj_i < arr_i else arr_i
        open_ch, close_ch = ("{", "}") if start == obj_i else ("[", "]")

    # 対応する閉じ括弧を探す（文字列内を考慮）
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == open_ch:
            depth += 1
            continue
        if ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return text[start:]


def _escape_control_chars_in_json_strings(text: str) -> str:
    """
    JSON文字列内に混入した生の改行/タブ等をエスケープする。
    LLMが出力した不正なJSONを修復するために使用。
    """
    if not text:
        return ""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            # 制御文字をエスケープ
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue

        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _repair_json_like_text(text: str) -> str:
    """
    LLMが生成しがちな「ほぼJSON」を、最低限パースできるように整形する。
    スマートクォートの置換、制御文字のエスケープ、末尾カンマの除去を行う。
    """
    if not text:
        return ""
    s = text.strip()
    # スマートクォート対策
    s = s.replace(""", '"').replace(""", '"')
    # 文字列内の改行/タブ等
    s = _escape_control_chars_in_json_strings(s)
    # 末尾カンマ
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _finish_reason(resp: Any) -> str:
    """レスポンスからfinish_reasonを取得する。"""
    try:
        choice = resp.choices[0]
        finish_reason = getattr(choice, "finish_reason", None) or choice.get("finish_reason")
        return str(finish_reason or "")
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class StreamDelta:
    """ストリーミングの差分テキストとfinish_reasonをまとめたデータ。"""

    text: str
    finish_reason: Optional[str] = None


@dataclass(frozen=True)
class ReplyWebSearchConfig:
    """最終応答（SYNC_CONVERSATION）で使うWeb検索パラメータ。"""

    mode: str
    web_search_options: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    extra_body: Optional[Dict[str, Any]] = None


class LlmRequestPurpose:
    """LLM呼び出しの処理目的（ログ用途のラベル）。"""

    # --- 同期（API処理中） ---
    SYNC_CONVERSATION = "【同期】＜＜ 会話応答作成 ＞＞"
    SYNC_SEARCH_SELECT = "【同期】＜＜ 検索候補の選別（SearchResultPack） ＞＞"
    SYNC_NOTIFICATION = "【同期】＜＜ 通知（受信報告） ＞＞"
    SYNC_META_REQUEST = "【同期】＜＜ メタ要求対応 ＞＞"
    SYNC_IMAGE_DETAIL = "【同期】＜＜ 画像説明生成（詳細） ＞＞"
    SYNC_IMAGE_SUMMARY_CHAT = "【同期】＜＜ 画像要約（チャット） ＞＞"
    SYNC_IMAGE_SUMMARY_NOTIFICATION = "【同期】＜＜ 画像要約（通知） ＞＞"
    SYNC_IMAGE_SUMMARY_META_REQUEST = "【同期】＜＜ 画像要約（メタ要求） ＞＞"
    SYNC_IMAGE_SUMMARY_DESKTOP_WATCH = "【同期】＜＜ 画像要約（デスクトップウォッチ） ＞＞"
    SYNC_DESKTOP_WATCH = "【同期】＜＜ デスクトップウォッチ ＞＞"
    SYNC_REMINDER = "【同期】＜＜ リマインダー ＞＞"
    SYNC_RETRIEVAL_QUERY_EMBEDDING = "【同期】＜＜ 記憶検索用クエリの埋め込み取得 ＞＞"

    # --- 非同期（Worker） ---
    ASYNC_EVENT_EMBEDDING = "【WORKER】＜＜ 出来事ログ埋め込み（events） ＞＞"
    ASYNC_STATE_EMBEDDING = "【WORKER】＜＜ 状態埋め込み（state） ＞＞"
    ASYNC_EVENT_AFFECT_EMBEDDING = "【WORKER】＜＜ 感情埋め込み（event_affect） ＞＞"
    ASYNC_WRITE_PLAN = "【WORKER】＜＜ 記憶更新計画作成（WritePlan） ＞＞"
    ASYNC_EVENT_ASSISTANT_SUMMARY = "【WORKER】＜＜ アシスタント本文要約（events） ＞＞"
    ASYNC_STATE_LINKS = "【WORKER】＜＜ 状態リンク生成（state_links） ＞＞"
    ASYNC_AUTONOMY_DELIBERATION = "【WORKER】＜＜ 自発行動の意思決定（Deliberation） ＞＞"
    ASYNC_AUTONOMY_WEB_ACCESS = "【WORKER】＜＜ 自発行動のWeb調査（web_access） ＞＞"


def _normalize_purpose(purpose: str) -> str:
    """空文字などを吸収し、ログに最低限の目的ラベルを残す。"""
    label = str(purpose or "").strip()
    if not label:
        return "＜＜ 不明 ＞＞"

    # --- 既に＜＜＞＞形式（またはプレフィックス付き）ならそのまま ---
    if label.endswith("＞＞") and "＜＜" in label:
        left = label.find("＜＜")
        right = label.rfind("＞＞")
        if 0 <= left < right:
            return label

    # --- 呼び出し側が書式を揃えるのを正としつつ、崩れた場合だけ最低限包む ---
    return f"＜＜ {label} ＞＞"


class LlmClient:
    """
    LLM APIクライアント。
    LiteLLMを使用してLLM APIを呼び出し、会話応答やJSON生成を行う。
    """

    # ログ出力時のプレビュー文字数（デフォルト）
    _DEBUG_PREVIEW_CHARS = 5000

    def __init__(
        self,
        model: str,
        embedding_model: str,
        image_model: str,
        api_key: Optional[str] = None,
        embedding_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        embedding_base_url: Optional[str] = None,
        image_llm_base_url: Optional[str] = None,
        image_model_api_key: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        reply_web_search_enabled: bool = True,
        max_tokens: int = 4096,
        max_tokens_vision: int = 4096,
        image_timeout_seconds: int = 60,
        timeout_seconds: int = 30,
        stream_timeout_seconds: int = 60,
    ):
        """
        LLMクライアントを初期化する。

        Args:
            model: メインLLMモデル名
            embedding_model: 埋め込みモデル名
            image_model: 画像認識用モデル名
            api_key: LLM APIキー
            embedding_api_key: 埋め込みAPIキー（未指定時はapi_keyを使用）
            llm_base_url: LLM APIベースURL（ローカルLLM等のOpenAI互換向け）
            embedding_base_url: 埋め込みAPIベースURL（ローカルLLM等のOpenAI互換向け）
            image_llm_base_url: 画像モデルAPIベースURL（ローカルLLM等のOpenAI互換向け）
            image_model_api_key: 画像モデルAPIキー
            reasoning_effort: 推論詳細度設定（推論モデル用）
            reply_web_search_enabled: 最終応答（SYNC_CONVERSATION）でWeb検索を有効化するか
            max_tokens: 通常時の最大トークン数
            max_tokens_vision: 画像認識時の最大トークン数
            image_timeout_seconds: 画像処理タイムアウト秒数
            timeout_seconds: LLM API（非ストリーム）のタイムアウト秒数
            stream_timeout_seconds: LLM API（ストリーム開始）のタイムアウト秒数
        """
        self.logger = logging.getLogger(__name__)
        # NOTE: LLM送受信ログは出力先ごとにロガーを分ける。
        self.io_console_logger = logging.getLogger("cocoro_ghost.llm_io.console")
        self.io_file_logger = logging.getLogger("cocoro_ghost.llm_io.file")
        self.model = model
        self.embedding_model = embedding_model
        self.image_model = image_model
        self.api_key = api_key
        self.embedding_api_key = embedding_api_key or api_key
        self.llm_base_url = llm_base_url
        self.embedding_base_url = embedding_base_url
        self.image_llm_base_url = image_llm_base_url
        self.image_model_api_key = image_model_api_key or api_key
        self.reasoning_effort = reasoning_effort
        self.reply_web_search_enabled = bool(reply_web_search_enabled)
        self.max_tokens = max_tokens
        self.max_tokens_vision = max_tokens_vision
        self.image_timeout_seconds = image_timeout_seconds
        # NOTE:
        # - 外部LLMがハング/停滞するとAPI応答が返らなくなるため、上限を設ける。
        # - stream_timeout_seconds は「ストリーム開始（最初の接続確立）」までの待ち上限として使う。
        self.timeout_seconds = int(timeout_seconds)
        self.stream_timeout_seconds = int(stream_timeout_seconds)

    def _get_llm_log_level(self) -> str:
        """設定から llm_log_level を取得する。"""
        try:
            from cocoro_ghost.config import get_config_store

            return get_config_store().toml_config.llm_log_level
        except Exception:  # noqa: BLE001
            return "INFO"

    def _get_llm_log_max_chars(self) -> tuple[int, int]:
        """設定から LLM送受信ログの最大文字数を取得する。"""
        try:
            from cocoro_ghost.config import get_config_store

            toml_config = get_config_store().toml_config
            return (
                int(toml_config.llm_log_console_max_chars),
                int(toml_config.llm_log_file_max_chars),
            )
        except Exception:  # noqa: BLE001
            return (self._DEBUG_PREVIEW_CHARS, self._DEBUG_PREVIEW_CHARS)

    def _get_llm_log_value_max_chars(self) -> tuple[int, int]:
        """設定から LLM送受信ログのValue最大文字数を取得する。"""
        try:
            from cocoro_ghost.config import get_config_store

            toml_config = get_config_store().toml_config
            return (
                int(toml_config.llm_log_console_value_max_chars),
                int(toml_config.llm_log_file_value_max_chars),
            )
        except Exception:  # noqa: BLE001
            return (500, 2000)

    def _is_log_file_enabled(self) -> bool:
        """ファイルログの有効/無効を取得する。"""
        try:
            from cocoro_ghost.config import get_config_store

            return bool(get_config_store().toml_config.log_file_enabled)
        except Exception:  # noqa: BLE001
            return False

    def _log_llm_info(self, message: str, *args: Any) -> None:
        """LLM送受信のINFOログを出力する。"""
        self.io_console_logger.info(message, *args)
        if self._is_log_file_enabled():
            self.io_file_logger.info(message, *args)

    def _log_llm_error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """LLM送受信のERRORログを出力する。"""
        self.io_console_logger.error(message, *args, **kwargs)
        if self._is_log_file_enabled():
            self.io_file_logger.error(message, *args, **kwargs)

    # NOTE:
    # - LLM側の「コンテキスト長（入力トークン）超過」は運用上の頻出トラブルなので、
    #   例外種別やメッセージから判定し、原因が一目で分かる日本語ログを残す。
    # - 末尾文言はユーザー指定に合わせて固定で付与する。
    def _is_context_window_exceeded(self, exc: Exception) -> bool:
        """
        例外が「コンテキスト長超過（入力トークン過多）」由来かを判定する。

        LiteLLMはプロバイダ差分を吸収するため、例外型が揺れる可能性がある。
        そのため、型判定＋メッセージ判定の両方で検出する。
        """
        # まず LiteLLM の専用例外を優先する。
        try:
            from litellm import exceptions as litellm_exceptions

            if isinstance(exc, litellm_exceptions.ContextWindowExceededError):
                return True
            if isinstance(exc, litellm_exceptions.BadRequestError):
                msg = str(exc).lower()
                if "context window" in msg or "maximum context length" in msg or "context length" in msg:
                    return True
        except Exception:  # noqa: BLE001
            pass

        # フォールバック: メッセージ内容で検出する（プロバイダ/SDK差分対策）。
        msg = str(exc).lower()
        keywords = (
            "context_window_exceeded",
            "context window",
            "maximum context length",
            "context length",
            "too many tokens",
            "prompt is too long",
            "input is too long",
        )
        return any(k in msg for k in keywords)

    def _log_context_window_exceeded_error(
        self,
        *,
        purpose_label: str,
        kind: str,
        elapsed_ms: int,
        approx_chars: int | None,
        messages_count: int | None,
        stream: bool | None,
        exc: Exception,
    ) -> None:
        """
        コンテキスト長超過（入力トークン過多）に特化したERRORログを出力する。

        目的:
        - 通常の「LLM request failed」よりも原因を明確にし、運用で探しやすくする。
        """
        self._log_llm_error(
            "トークン予算（コンテキスト長）を超過したため、LLMリクエストに失敗しました: purpose=%s kind=%s stream=%s messages=%s approx_chars=%s ms=%s error=%s。最大トークンを増やすか会話履歴数を減らしてください",
            purpose_label,
            str(kind),
            stream,
            messages_count,
            approx_chars,
            int(elapsed_ms),
            str(exc),
            exc_info=exc,
        )

    def _log_llm_payload(
        self,
        label: str,
        payload: Any,
        *,
        llm_log_level: str,
    ) -> None:
        """LLM送受信のpayloadログを出力する。"""
        console_max_chars, file_max_chars = self._get_llm_log_max_chars()
        console_max_value_chars, file_max_value_chars = self._get_llm_log_value_max_chars()
        log_llm_payload(
            self.io_console_logger,
            label,
            payload,
            max_chars=console_max_chars,
            max_value_chars=console_max_value_chars,
            llm_log_level=llm_log_level,
        )
        if self._is_log_file_enabled():
            log_llm_payload(
                self.io_file_logger,
                label,
                payload,
                max_chars=file_max_chars,
                max_value_chars=file_max_value_chars,
                llm_log_level=llm_log_level,
            )

    def _build_completion_kwargs(
        self,
        model: str,
        messages: List[Dict],
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        timeout: Optional[int] = None,
        stream: bool = False,
        web_search_options: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """
        completion API呼び出し用のkwargsを構築する。
        """
        # 基本パラメータ
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }

        # オプションパラメータを追加
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["api_base"] = base_url
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if response_format:
            kwargs["response_format"] = response_format
        if timeout:
            kwargs["timeout"] = timeout
        if web_search_options:
            kwargs["web_search_options"] = web_search_options
        if tools:
            kwargs["tools"] = tools

        # --- 推論（reasoning/thinking）パラメータ ---
        # NOTE:
        # - OpenAIは推論系モデル向けに effort を制御するパラメータを持つ（モデル/エンドポイントで形が異なる）。
        #   このプロジェクトでは LiteLLM の `reasoning_effort` を使って統一する。
        # - OpenRouter は `reasoning.effort` を統一パラメータとして提供し、各プロバイダへマップする。
        # - `extra_body` は OpenAI互換APIへ「非標準パラメータ」を渡すための逃げ道。
        #   OpenRouter のドキュメントでも extra_body.reasoning の例が示されている。
        def _normalize_reasoning_effort(value: Optional[str]) -> Optional[str]:
            """reasoning_effort を正規化する（空なら None）。"""
            cleaned = str(value or "").strip().lower()
            return cleaned or None

        def _normalize_openrouter_reasoning_effort(value: str) -> str:
            """
            OpenRouter の reasoning.effort に合わせて値を正規化する。

            OpenRouter は low/medium/high/xhigh を受け付けるため、それ以外は
            なるべく最小の low に寄せる（落ちにくさ優先）。
            """
            v = str(value or "").strip().lower()
            if v in {"low", "medium", "high", "xhigh"}:
                return v
            if v in {"minimal", "none"}:
                return "low"
            return v

        def _openai_model_id_from_litellm_model(litellm_model: str) -> str:
            """LiteLLMの model 文字列から OpenAI側の model id を取り出す。"""
            cleaned = str(litellm_model or "").strip().lower()

            # --- LiteLLMの経路プレフィックスを除去する ---
            if cleaned.startswith("openai/"):
                cleaned = cleaned.removeprefix("openai/")

            # --- OpenRouter embeddings 等で現れる二重 openai/ を吸収する ---
            if cleaned.startswith("openai/"):
                cleaned = cleaned.removeprefix("openai/")

            # --- Responses API 経由指定（openai/responses/<model>）を吸収する ---
            if cleaned.startswith("responses/"):
                cleaned = cleaned.removeprefix("responses/")

            return cleaned

        def _default_reasoning_effort_for_openai_model_id(openai_model_id: str) -> Optional[str]:
            """OpenAIの model id から、最小の reasoning_effort を返す。"""
            model_id = str(openai_model_id or "").strip().lower()
            if not model_id:
                return None

            # --- GPT-5.2 Pro: supported values = medium/high/xhigh ---
            if model_id.startswith("gpt-5.2-pro"):
                return "medium"

            # --- GPT-5 Pro: supported values = high ---
            if model_id.startswith("gpt-5-pro"):
                return "high"

            # --- GPT-5.2: supported values = none/low/medium/high/xhigh ---
            if model_id.startswith("gpt-5.2"):
                return "none"

            # --- GPT-5.1: supported values = none/low/medium/high ---
            if model_id.startswith("gpt-5.1"):
                return "none"

            # --- GPT-5: supported values = minimal/low/medium/high ---
            if model_id.startswith("gpt-5"):
                return "minimal"

            # --- o-series: supported values はモデルごとに差があるため、保守的に low を使う ---
            if re.match(r"^o\d", model_id):
                return "low"

            return None

        def _default_reasoning_effort_for_model(litellm_model: str) -> Optional[str]:
            """reasoning_effort 未指定時の既定値（最低レベル）を返す。"""
            model_cleaned = str(litellm_model or "").strip().lower()
            if not model_cleaned:
                return None

            # --- OpenRouter: reasoning.effort の最小は low（モデル側が未対応でも丸められる） ---
            if model_cleaned.startswith("openrouter/"):
                return "low"

            # --- OpenAI: 推論モデルにだけ既定値を入れる（非推論モデルに投げて落とさない） ---
            if model_cleaned.startswith("openai/"):
                model_id = _openai_model_id_from_litellm_model(model_cleaned)
                return _default_reasoning_effort_for_openai_model_id(model_id)

            # --- Gemini: LiteLLM の reasoning_effort は thinkingConfig にマップされる ---
            if model_cleaned.startswith(("google/", "gemini/")):
                _, _, model_id = model_cleaned.partition("/")
                if model_id.startswith("gemini-"):
                    return "minimal"
                return None

            return None

        model_name = str(model or "").strip()
        model_name_lower = model_name.lower()
        reasoning_effort = _normalize_reasoning_effort(self.reasoning_effort)
        default_effort = _default_reasoning_effort_for_model(model_name)
        effort_to_send = reasoning_effort or default_effort

        provider_extra_body: Dict[str, Any] = {}

        # --- OpenRouter: unified reasoning param ---
        if model_name_lower.startswith("openrouter/") and effort_to_send:
            provider_extra_body["reasoning"] = {"effort": str(_normalize_openrouter_reasoning_effort(effort_to_send))}

        # --- OpenAI: Chat Completions reasoning_effort param ---
        # NOTE: OpenAI以外へ不用意に投げると unknown parameter で落ちる可能性があるため、
        #       "openai/" prefix の場合だけ付与する。
        if model_name_lower.startswith("openai/") and effort_to_send:
            if _default_reasoning_effort_for_model(model_name_lower) is not None or reasoning_effort is not None:
                kwargs["reasoning_effort"] = str(effort_to_send)

        # --- Gemini: LiteLLM 統一パラメータ（reasoning_effort） ---
        # NOTE:
        # - Gemini は内部で thinkingConfig にマップされる。
        # - OpenRouter 経由は extra_body.reasoning を使うため、ここは直呼び（google/・gemini/）のみ。
        if model_name_lower.startswith(("google/", "gemini/")) and effort_to_send:
            if _default_reasoning_effort_for_model(model_name_lower) is not None or reasoning_effort is not None:
                kwargs["reasoning_effort"] = str(effort_to_send)

        # --- プロバイダ指定のextra_bodyと、呼び出し側指定のextra_bodyを統合する ---
        merged_extra_body: Dict[str, Any] = {}
        if provider_extra_body:
            merged_extra_body.update(provider_extra_body)
        if extra_body:
            merged_extra_body.update(extra_body)
        if merged_extra_body:
            kwargs["extra_body"] = merged_extra_body

        return kwargs

    def _is_openrouter_route(self, *, model_name: str, base_url: Optional[str]) -> bool:
        """
        このリクエストが OpenRouter 経由かを判定する。

        判定条件:
            - model が `openrouter/` で始まる
            - または base_url に `openrouter.ai` を含む
        """
        model_lower = str(model_name or "").strip().lower()
        if model_lower.startswith("openrouter/"):
            return True
        base = str(base_url or "").strip().lower()
        return "openrouter.ai" in base

    def _resolve_reply_web_search_config(
        self,
        *,
        purpose: str,
        model: str,
        base_url: Optional[str],
    ) -> Optional[ReplyWebSearchConfig]:
        """
        最終応答（SYNC_CONVERSATION）で使うWeb検索設定を返す。

        注意:
            - 「検索は3でのみON」の方針に合わせ、SYNC_CONVERSATION以外ではNoneを返す。
            - 対象外プロバイダは無言で落とさず、例外で明示する。
        """
        # --- 設定OFF時は最終応答でもWeb検索を使わない ---
        if not bool(self.reply_web_search_enabled):
            return None

        # --- 3以外（選別/埋め込み/外部応答）ではWeb検索を使わない ---
        if str(purpose or "").strip() != LlmRequestPurpose.SYNC_CONVERSATION:
            return None

        return self._resolve_web_search_config_for_model(model=model, base_url=base_url)

    def _resolve_autonomy_web_search_config(
        self,
        *,
        model: str,
        base_url: Optional[str],
    ) -> ReplyWebSearchConfig:
        """
        自発行動（autonomy）用のWeb検索設定を返す。

        方針:
            - `/api/chat` 側の `reply_web_search_enabled` と完全分離する。
            - 自発行動は purpose 専用経路でのみ呼び出す。
        """
        return self._resolve_web_search_config_for_model(model=model, base_url=base_url)

    def _resolve_web_search_config_for_model(
        self,
        *,
        model: str,
        base_url: Optional[str],
    ) -> ReplyWebSearchConfig:
        """モデル/経路に応じたWeb検索設定を返す（共通実装）。"""

        # --- モデル文字列を正規化して判定する ---
        model_name = str(model or "").strip()
        model_name_lower = model_name.lower()

        # --- OpenRouter経由: plugin web を有効化し、検索コンテキストを最大化する ---
        if self._is_openrouter_route(model_name=model_name, base_url=base_url):
            return ReplyWebSearchConfig(
                mode="openrouter-plugin-web",
                web_search_options={"search_context_size": "high"},
                extra_body={"plugins": [{"id": "web"}]},
            )

        # --- xAI直呼び: web_search ツールを使う ---
        if model_name_lower.startswith("xai/"):
            return ReplyWebSearchConfig(
                mode="xai-web-search-tool",
                tools=[{"type": "web_search"}],
            )

        # --- OpenAI直呼び: web_search_options を使う ---
        if model_name_lower.startswith("openai/"):
            return ReplyWebSearchConfig(
                mode="openai-web-search-options",
                web_search_options={"search_context_size": "high"},
            )

        # --- Gemini直呼び: web_search_options（LiteLLMがGoogle Searchへ変換）を使う ---
        if model_name_lower.startswith(("google/", "gemini/")):
            return ReplyWebSearchConfig(
                mode="gemini-google-search",
                web_search_options={"search_context_size": "high"},
            )

        # --- 方針上、対象外は明示的にエラーにする（無言で検索OFFにしない） ---
        raise ValueError(
            "Web検索は openrouter/*, xai/*, openai/*, google/*, gemini/* のみ対応です"
        )

    def generate_reply_response(
        self,
        system_prompt: str,
        conversation: List[Dict[str, str]],
        purpose: str,
        stream: bool = False,
    ):
        """
        会話応答を生成する（Responseオブジェクト or ストリーム）。
        システムプロンプトと会話履歴からLLMに応答を生成させる。
        purpose はログに出す処理目的ラベル。
        """
        # メッセージ配列を構築
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})

        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)
        start = time.perf_counter()
        web_search_cfg = self._resolve_reply_web_search_config(
            purpose=purpose,
            model=self.model,
            base_url=self.llm_base_url,
        )

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            stream=stream,
            web_search_options=(web_search_cfg.web_search_options if web_search_cfg else None),
            tools=(web_search_cfg.tools if web_search_cfg else None),
            extra_body=(web_search_cfg.extra_body if web_search_cfg else None),
            # NOTE:
            # - stream=False は「レスポンス全体」が返るまで待つため、短めの上限を設ける。
            # - stream=True は開始が返らない（=無限待ち）を防ぐ目的で、開始までの上限を設ける。
            timeout=(self.stream_timeout_seconds if stream else self.timeout_seconds),
        )

        msg_count = len(messages)
        approx_chars = _estimate_text_chars(messages)
        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM request 送信 %s kind=chat stream=%s messages=%s 文字数=%s",
                purpose_label,
                bool(stream),
                msg_count,
                approx_chars,
            )
            # --- 最終応答では、どの方式でWeb検索を有効化したかを明示ログに残す ---
            if web_search_cfg is not None:
                self._log_llm_info(
                    "LLM web search 有効化 %s mode=%s",
                    purpose_label,
                    str(web_search_cfg.mode),
                )
        self._log_llm_payload("LLM request (chat)", _sanitize_for_llm_log(kwargs), llm_log_level=llm_log_level)

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                # コンテキスト長超過は原因を明確に区別する（ERROR）。
                if self._is_context_window_exceeded(exc):
                    self._log_context_window_exceeded_error(
                        purpose_label=purpose_label,
                        kind="chat",
                        elapsed_ms=elapsed_ms,
                        approx_chars=approx_chars,
                        messages_count=msg_count,
                        stream=bool(stream),
                        exc=exc,
                    )
                else:
                    self._log_llm_error(
                        "LLM request failed %s kind=chat stream=%s messages=%s ms=%s error=%s",
                        purpose_label,
                        bool(stream),
                        msg_count,
                        elapsed_ms,
                        str(exc),
                        exc_info=exc,
                    )
            raise

        # ストリームは呼び出し側が受信するため、ここでは受信ログを出さない。
        if stream:
            return resp

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = _first_choice_content(resp)
        finish_reason = _finish_reason(resp)
        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM response 受信 %s kind=chat stream=%s finish_reason=%s chars=%s ms=%s",
                purpose_label,
                False,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        self._log_llm_payload(
            "LLM response (chat)",
            _sanitize_for_llm_log(
                {
                    "model": self.model,
                    "finish_reason": finish_reason,
                    "content": content,
                }
            ),
            llm_log_level=llm_log_level,
        )
        return resp

    def generate_json_response(
        self,
        *,
        system_prompt: str,
        input_text: str,
        purpose: str,
        max_tokens: Optional[int] = None,
        model_override: Optional[str] = None,
    ):
        """JSON（json_object）を生成する（Responseオブジェクト）。

        INFO: 送受信した事実（メタ情報）のみ
        DEBUG: 内容も出す（マスク＋トリミング）
        purpose はログに出す処理目的ラベル。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text},
        ]

        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)
        start = time.perf_counter()

        requested_max_tokens = max_tokens or self.max_tokens
        model_for_request = str(model_override or self.model)
        kwargs = self._build_completion_kwargs(
            model=model_for_request,
            messages=messages,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=requested_max_tokens,
            response_format={"type": "json_object"},
            timeout=self.timeout_seconds,
        )

        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM request 送信 %s kind=json stream=%s messages=%s 文字数=%s",
                purpose_label,
                False,
                len(messages),
                _estimate_text_chars(messages),
            )
        self._log_llm_payload("LLM request (json)", _sanitize_for_llm_log(kwargs), llm_log_level=llm_log_level)

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                # コンテキスト長超過は原因を明確に区別する（ERROR）。
                if self._is_context_window_exceeded(exc):
                    self._log_context_window_exceeded_error(
                        purpose_label=purpose_label,
                        kind="json",
                        elapsed_ms=elapsed_ms,
                        approx_chars=_estimate_text_chars(messages),
                        messages_count=len(messages),
                        stream=False,
                        exc=exc,
                    )
                else:
                    self._log_llm_error(
                        "LLM request failed %s kind=json ms=%s error=%s",
                        purpose_label,
                        elapsed_ms,
                        str(exc),
                        exc_info=exc,
                    )
            raise

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = _first_choice_content(resp)
        finish_reason = _finish_reason(resp)
        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM response 受信 %s kind=json finish_reason=%s chars=%s ms=%s",
                purpose_label,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        self._log_llm_payload(
            "LLM response (json)",
            _sanitize_for_llm_log({"finish_reason": finish_reason, "content": content}),
            llm_log_level=llm_log_level,
        )
        return resp

    def generate_reply_response_with_web_search(
        self,
        *,
        system_prompt: str,
        conversation: List[Dict[str, str]],
        purpose: str,
        stream: bool = False,
    ):
        """
        自発行動（autonomy）向けに、常時Web検索有効で会話応答を生成する。

        注意:
            - `/api/chat` の最終応答経路とは分離して運用する。
            - purpose は監査ログのラベルに使う。
        """
        # --- メッセージ配列を構築 ---
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})

        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)
        start = time.perf_counter()
        web_search_cfg = self._resolve_autonomy_web_search_config(
            model=self.model,
            base_url=self.llm_base_url,
        )

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            stream=stream,
            web_search_options=(web_search_cfg.web_search_options if web_search_cfg else None),
            tools=(web_search_cfg.tools if web_search_cfg else None),
            extra_body=(web_search_cfg.extra_body if web_search_cfg else None),
            timeout=(self.stream_timeout_seconds if stream else self.timeout_seconds),
        )

        msg_count = len(messages)
        approx_chars = _estimate_text_chars(messages)
        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM request 送信 %s kind=chat+web stream=%s messages=%s 文字数=%s",
                purpose_label,
                bool(stream),
                msg_count,
                approx_chars,
            )
            self._log_llm_info(
                "LLM web search 有効化 %s mode=%s",
                purpose_label,
                str(web_search_cfg.mode),
            )
        self._log_llm_payload("LLM request (chat+web)", _sanitize_for_llm_log(kwargs), llm_log_level=llm_log_level)

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                if self._is_context_window_exceeded(exc):
                    self._log_context_window_exceeded_error(
                        purpose_label=purpose_label,
                        kind="chat+web",
                        elapsed_ms=elapsed_ms,
                        approx_chars=approx_chars,
                        messages_count=msg_count,
                        stream=bool(stream),
                        exc=exc,
                    )
                else:
                    self._log_llm_error(
                        "LLM request failed %s kind=chat+web stream=%s messages=%s ms=%s error=%s",
                        purpose_label,
                        bool(stream),
                        msg_count,
                        elapsed_ms,
                        str(exc),
                        exc_info=exc,
                    )
            raise

        # --- ストリームは呼び出し側へ返す ---
        if stream:
            return resp

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = _first_choice_content(resp)
        finish_reason = _finish_reason(resp)
        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM response 受信 %s kind=chat+web stream=%s finish_reason=%s chars=%s ms=%s",
                purpose_label,
                False,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        self._log_llm_payload(
            "LLM response (chat+web)",
            _sanitize_for_llm_log(
                {
                    "model": self.model,
                    "finish_reason": finish_reason,
                    "content": content,
                }
            ),
            llm_log_level=llm_log_level,
        )
        return resp

    def generate_embedding(
        self,
        texts: List[str],
        purpose: str,
        images: Optional[List[bytes]] = None,
    ) -> List[List[float]]:
        """
        テキストの埋め込みベクトルを生成する。
        複数テキストを一括処理し、各テキストに対応するベクトルを返す。
        purpose はログに出す処理目的ラベル。
        """
        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)
        start = time.perf_counter()
        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM request 送信 %s kind=embedding 文字数=%s",
                purpose_label,
                sum(len(t or "") for t in texts),
            )
        # NOTE: embedding入力は漏洩しやすいので、DEBUGでもトリミングされる前提で出す。
        # inputキーは持たせず、配列のままログに出す。
        self._log_llm_payload(
            "LLM request (embedding)",
            _sanitize_for_llm_log(texts),
            llm_log_level=llm_log_level,
        )

        # --- Embeddingの呼び方を整形する ---
        # OpenRouter では `model="openrouter/<slug>"` の形式で設定する。
        # ただし LiteLLM の provider=openrouter は embeddings に対応していないため、
        # OpenRouter を OpenAI互換エンドポイントとして（openai/）呼び出す。
        model_for_request = self.embedding_model
        api_base_for_request = _get_embedding_api_base(
            embedding_model=self.embedding_model,
            embedding_base_url=self.embedding_base_url,
        )

        if str(self.embedding_model or "").strip().startswith("openrouter/"):
            # --- OpenRouter の埋め込みは OpenAI互換として呼び出す（api_base を自動設定） ---
            openrouter_slug = _openrouter_slug_from_model(self.embedding_model)
            _validate_openrouter_embedding_slug(openrouter_slug)
            model_for_request = _to_openai_compatible_model_for_slug(openrouter_slug)

        kwargs = {
            "model": model_for_request,
            "input": texts,
        }

        # --- OpenAI互換(=OpenRouter含む) embeddings の仕様に合わせる ---
        # NOTE:
        # - OpenAI互換 embeddings では `encoding_format` が enum(float|base64) でバリデーションされる。
        # - 一方、Gemini等のプロバイダ直呼びでは `encoding_format` 自体が未対応で弾かれる。
        # そのため model が OpenAI互換（openai/）のときだけ明示指定する。
        if str(model_for_request or "").strip().startswith("openai/"):
            kwargs["encoding_format"] = "float"
        if self.embedding_api_key:
            kwargs["api_key"] = self.embedding_api_key
        if api_base_for_request:
            kwargs["api_base"] = api_base_for_request
        # NOTE:
        # - 埋め込み取得がハングすると記憶検索が止まり、結果としてチャット応答が返らなくなるため上限を設ける。
        kwargs["timeout"] = int(self.timeout_seconds)

        try:
            resp = litellm.embedding(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                # コンテキスト長超過は原因を明確に区別する（ERROR）。
                if self._is_context_window_exceeded(exc):
                    self._log_context_window_exceeded_error(
                        purpose_label=purpose_label,
                        kind="embedding",
                        elapsed_ms=elapsed_ms,
                        approx_chars=sum(len(t or "") for t in texts),
                        messages_count=None,
                        stream=None,
                        exc=exc,
                    )
                else:
                    self._log_llm_error(
                        "LLM request failed %s kind=embedding ms=%s error=%s",
                        purpose_label,
                        elapsed_ms,
                        str(exc),
                        exc_info=exc,
                    )
            raise

        # レスポンス形式に応じて埋め込みベクトルを取り出す。
        try:
            out = [item["embedding"] for item in resp["data"]]
        except Exception:  # noqa: BLE001
            out = [item.embedding for item in resp.data]

        # 受信の事実だけはINFO/DEBUGで明示する（内容は出さない）。
        if llm_log_level != "OFF":
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            self._log_llm_info(
                "LLM response 受信 %s kind=embedding ms=%s",
                purpose_label,
                elapsed_ms,
            )
            if llm_log_level == "DEBUG":
                self.io_console_logger.debug(
                    "LLM response 受信 kind=embedding (payload omitted)",
                )
                if self._is_log_file_enabled():
                    self.io_file_logger.debug(
                        "LLM response 受信 kind=embedding (payload omitted)",
                    )
        return out

    def generate_image_summary(
        self,
        images: List[bytes],
        purpose: str,
        *,
        mime_types: Optional[List[str]] = None,
        max_chars: Optional[int] = None,
        best_effort: bool = False,
    ) -> List[str]:
        """
        画像の要約を生成する。
        各画像をVision LLMで解析し、日本語で説明テキストを返す。
        purpose はログに出す処理目的ラベル。

        Args:
            images: 画像バイナリ（bytes）の配列。
            purpose: ログ用途の処理目的ラベル。
            mime_types: data URI に埋め込む MIME タイプ（images と同じ長さ）。省略時は image/png とする。
            max_chars: 1画像あたりの最大文字数（必要なら指定）。指定された場合は指示＋切り詰めを行う。
            best_effort: True の場合、1枚の失敗で全体を失敗扱いにせず、失敗した画像は "" を返して続行する。
        """
        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)

        # --- MIMEリストを正規化する ---
        # NOTE:
        # - image_url の data URI では MIME が重要になり得るため、入力に合わせる。
        # - 呼び出し側で不明なら image/png とする。
        if mime_types is None:
            mime_list = ["image/png" for _ in images]
        else:
            if len(mime_types) != len(images):
                raise ValueError("mime_types length must match images length")
            mime_list = [str(x or "").strip().lower() or "image/png" for x in mime_types]

        def _build_vision_task_text(*, is_desktop_watch_summary: bool) -> tuple[str, str]:
            """
            Visionタスクの system と「タスク定義（語彙）」を返す。

            NOTE:
                - visionモデルに「画像」と言うと、戻り文が「この画像は〜」に寄りやすい。
                - デスクトップウォッチは「画面（スクリーンショット）」として扱いたいので、
                  purpose に応じて語彙だけを差し替える。
            """
            # --- desktop_watch は「画面」語彙に寄せる ---
            if bool(is_desktop_watch_summary):
                system_text = "あなたは日本語で説明します。"
                instruction_head = "これはデスクトップ画面（スクリーンショット）です。"
                instruction_body = "画面の内容を詳細に説明してください。"
                return system_text, " ".join([instruction_head, instruction_body]).strip()

            # --- 通常（画像） ---
            system_text = "あなたは日本語で画像を説明します。"
            instruction_body = "この画像を詳細に説明してください。"
            return system_text, str(instruction_body)

        def _parse_summaries_json(*, content: str, expected_len: int) -> list[str]:
            """
            VisionのJSON出力（summaries配列）をパースする。

            期待フォーマット:
                {"summaries": ["...","..."]}
            """
            # --- まずは素直に JSON として読む ---
            raw = str(content or "").strip()
            obj: Any | None = None
            try:
                obj = json.loads(raw)
            except Exception:  # noqa: BLE001
                obj = None

            # --- JSONが前後に混ざった場合は、最初の {...} を雑に抽出して読む ---
            if obj is None:
                try:
                    i0 = raw.find("{")
                    i1 = raw.rfind("}")
                    if i0 >= 0 and i1 > i0:
                        obj = json.loads(raw[i0 : i1 + 1])
                except Exception:  # noqa: BLE001
                    obj = None

            if not isinstance(obj, dict):
                raise ValueError("vision JSON output is not an object")

            summaries = obj.get("summaries")
            if not isinstance(summaries, list):
                raise ValueError("vision JSON output missing 'summaries' array")

            out = [str(x or "").strip() for x in summaries]
            if int(len(out)) != int(expected_len):
                raise ValueError(f"vision summaries length mismatch expected={expected_len} got={len(out)}")
            return out

        def _process_one(image_bytes: bytes, mime: str) -> str:
            start = time.perf_counter()
            if llm_log_level != "OFF":
                self._log_llm_info(
                    "LLM request 送信 %s kind=vision image_bytes=%s",
                    purpose_label,
                    len(image_bytes),
                )
            # 画像をbase64エンコード
            b64 = base64.b64encode(image_bytes).decode("ascii")
            # --- 指示文（必要なら最大文字数を明示） ---
            # NOTE: 受信側の暴走対策として、後段でも念のため切り詰める。
            is_desktop_watch_summary = str(purpose or "").strip() == LlmRequestPurpose.SYNC_IMAGE_SUMMARY_DESKTOP_WATCH
            system_text, task_text = _build_vision_task_text(is_desktop_watch_summary=bool(is_desktop_watch_summary))

            instructions: list[str] = []
            instructions.append(str(task_text))
            if max_chars is not None and int(max_chars) > 0:
                instructions.append(f"必須: {int(max_chars)}文字以内。")
            instruction_text = " ".join(instructions).strip()
            messages = [
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction_text},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                },
            ]

            kwargs = self._build_completion_kwargs(
                model=self.image_model,
                messages=messages,
                api_key=self.image_model_api_key,
                base_url=self.image_llm_base_url,
                max_tokens=self.max_tokens_vision,
                timeout=self.image_timeout_seconds,
            )

            self._log_llm_payload("LLM request (vision)", _sanitize_for_llm_log(kwargs), llm_log_level=llm_log_level)

            try:
                resp = litellm.completion(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if llm_log_level != "OFF":
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    # コンテキスト長超過は原因を明確に区別する（ERROR）。
                    if self._is_context_window_exceeded(exc):
                        self._log_context_window_exceeded_error(
                            purpose_label=purpose_label,
                            kind="vision",
                            elapsed_ms=elapsed_ms,
                            approx_chars=_estimate_text_chars(messages),
                            messages_count=len(messages),
                            stream=False,
                            exc=exc,
                        )
                    else:
                        self._log_llm_error(
                            "LLM request failed %s kind=vision image_bytes=%s ms=%s error=%s",
                            purpose_label,
                            len(image_bytes),
                            elapsed_ms,
                            str(exc),
                            exc_info=exc,
                        )
                raise
            content = _first_choice_content(resp)
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self._log_llm_info(
                    "LLM response 受信 %s kind=vision chars=%s ms=%s",
                    purpose_label,
                    len(content or ""),
                    elapsed_ms,
                )
            self._log_llm_payload(
                "LLM response (vision)",
                _sanitize_for_llm_log({"content": content, "finish_reason": _finish_reason(resp)}),
                llm_log_level=llm_log_level,
            )
            out = str(content or "").strip()
            # LLMが文字数制限を守らない場合の防御として切り詰める
            if max_chars is not None and int(max_chars) > 0 and len(out) > int(max_chars):
                out = out[: int(max_chars)]
            return out

        def _process_many(images_bytes: list[bytes], mimes: list[str]) -> list[str]:
            """
            複数画像を「1回のVisionリクエスト」で要約する。

            目的:
                - 画像枚数が増えるほど、1枚ずつのLLM呼び出しは遅くなりやすい。
                - まとめて渡しつつ、返答は images の順番に対応した summaries 配列で返させる。
            """
            start = time.perf_counter()
            is_desktop_watch_summary = str(purpose or "").strip() == LlmRequestPurpose.SYNC_IMAGE_SUMMARY_DESKTOP_WATCH
            system_text, task_text = _build_vision_task_text(is_desktop_watch_summary=bool(is_desktop_watch_summary))

            # --- 画像をbase64エンコードして data URI 化する ---
            # NOTE:
            # - 画像は入力順（valid_images_bytes の順）で並べる。
            # - モデルには「この順番に対応する配列で返せ」と強く要求する。
            data_urls: list[str] = []
            total_image_bytes = 0
            for b, m in zip(images_bytes, mimes, strict=False):
                total_image_bytes += int(len(b))
                b64 = base64.b64encode(b).decode("ascii")
                data_urls.append(f"data:{m};base64,{b64}")

            # --- 指示文（JSON固定 + 配列長固定） ---
            # NOTE:
            # - response_format はプロバイダ差があるため、指示文でJSON固定を主にする。
            # - 出力は「画像ごとの説明」を配列に分離し、混ざらないようにする。
            expected_len = int(len(data_urls))
            instruction_lines: list[str] = [
                str(task_text),
                "",
                "重要: 出力はJSONオブジェクトのみ（前後に説明文やコードフェンスは禁止）。",
                "次のスキーマで出力する:",
                "{",
                '  "summaries": ["string", "..."]',
                "}",
                f"ルール: summaries の要素数は必ず {expected_len} 件で、入力画像の順番に対応させる。",
                "ルール: summaries[i] は画像(i+1)だけを説明し、他の画像の内容を混ぜない。",
            ]
            if max_chars is not None and int(max_chars) > 0:
                instruction_lines.append(f"ルール: 各 summaries[i] は必ず {int(max_chars)} 文字以内。")
            instruction_text = "\n".join([x for x in instruction_lines if str(x).strip()]).strip()

            # --- messages を組み立てる（画像ごとにラベルを入れて混線を防ぐ） ---
            user_content: list[dict[str, Any]] = [{"type": "text", "text": instruction_text}]
            for idx, url in enumerate(data_urls):
                user_content.append({"type": "text", "text": f"画像{int(idx) + 1}:"})
                user_content.append({"type": "image_url", "image_url": {"url": str(url)}})
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_content},
            ]

            if llm_log_level != "OFF":
                self._log_llm_info(
                    "LLM request 送信 %s kind=vision images=%s image_bytes_total=%s",
                    purpose_label,
                    expected_len,
                    total_image_bytes,
                )

            kwargs = self._build_completion_kwargs(
                model=self.image_model,
                messages=messages,
                api_key=self.image_model_api_key,
                base_url=self.image_llm_base_url,
                max_tokens=self.max_tokens_vision,
                timeout=self.image_timeout_seconds,
            )
            self._log_llm_payload("LLM request (vision)", _sanitize_for_llm_log(kwargs), llm_log_level=llm_log_level)

            try:
                resp = litellm.completion(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if llm_log_level != "OFF":
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    if self._is_context_window_exceeded(exc):
                        self._log_context_window_exceeded_error(
                            purpose_label=purpose_label,
                            kind="vision",
                            elapsed_ms=elapsed_ms,
                            approx_chars=_estimate_text_chars(messages),
                            messages_count=len(messages),
                            stream=False,
                            exc=exc,
                        )
                    else:
                        self._log_llm_error(
                            "LLM request failed %s kind=vision images=%s ms=%s error=%s",
                            purpose_label,
                            expected_len,
                            elapsed_ms,
                            str(exc),
                            exc_info=exc,
                        )
                raise

            content = _first_choice_content(resp)
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self._log_llm_info(
                    "LLM response 受信 %s kind=vision images=%s chars=%s ms=%s",
                    purpose_label,
                    expected_len,
                    len(content or ""),
                    elapsed_ms,
                )
            self._log_llm_payload(
                "LLM response (vision)",
                _sanitize_for_llm_log({"content": content, "finish_reason": _finish_reason(resp)}),
                llm_log_level=llm_log_level,
            )

            summaries = _parse_summaries_json(content=str(content or ""), expected_len=int(expected_len))
            # --- 文字数制限の最終防御 ---
            if max_chars is not None and int(max_chars) > 0:
                limit = int(max_chars)
                summaries = [str(s or "")[:limit].strip() for s in summaries]
            return summaries

        # --- 画像が1枚以下ならシンプルに処理する ---
        if len(images) <= 1:
            if not images:
                return []
            try:
                return [_process_one(images[0], mime_list[0])]
            except Exception:  # noqa: BLE001
                if best_effort:
                    return [""]
                raise

        # --- 2枚以上は、原則「1回のVisionリクエスト」にまとめる ---
        # NOTE:
        # - 画像枚数が増えるほど、リクエスト往復回数（=レイテンシ）が効くため。
        # - 失敗時は best_effort の方針に合わせてフォールバックする。
        try:
            return _process_many([bytes(b) for b in images], [str(m) for m in mime_list])
        except Exception:  # noqa: BLE001
            if not best_effort:
                raise

            # --- best_effort: 失敗した場合でも、可能な範囲で埋める（1枚ずつにフォールバック） ---
            out2: list[str] = []
            for idx, (b, m) in enumerate(zip(images, mime_list, strict=False)):
                try:
                    out2.append(_process_one(b, m))
                except Exception as exc:  # noqa: BLE001
                    if llm_log_level != "OFF":
                        self._log_llm_error(
                            "LLM request failed (best_effort fallback) %s kind=vision index=%s error=%s",
                            purpose_label,
                            idx,
                            str(exc),
                        )
                    out2.append("")
            return out2

    def response_to_dict(self, resp: Any) -> Dict[str, Any]:
        """Responseオブジェクトをログ/デバッグ用のdictに変換する。"""
        return _response_to_dict(resp)

    def response_content(self, resp: Any) -> str:
        """Responseから本文（choices[0].message.content）を取り出す。"""
        return _first_choice_content(resp)

    def response_json(self, resp: Any) -> Any:
        """
        LLM応答本文からJSONを抽出・修復しつつパースする。
        LLMが出力した不正なJSONも可能な限り修復してパースを試みる。
        """
        content = self.response_content(resp)
        candidate = _extract_first_json_value(content)
        if not candidate:
            raise ValueError("empty LLM content for JSON parsing")

        # まず通常のパースを試行
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc1:
            # 失敗した場合は修復を試みる
            repaired = _repair_json_like_text(candidate)
            try:
                parsed = json.loads(repaired)
                self.logger.warning(
                    "LLM JSON parse failed; parsed after repair (finish_reason=%s, error=%s)",
                    _finish_reason(resp),
                    exc1,
                )
                return parsed
            except json.JSONDecodeError as exc:
                # 失敗時はデバッグ用に内容を残す
                self.logger.debug("response_json parse failed: %s", exc)
                self.logger.debug("response_json content (raw): %s", truncate_for_log(content, self._DEBUG_PREVIEW_CHARS))
                self.logger.debug("response_json candidate: %s", truncate_for_log(candidate, self._DEBUG_PREVIEW_CHARS))
                self.logger.debug("response_json repaired: %s", truncate_for_log(repaired, self._DEBUG_PREVIEW_CHARS))
                raise

    def stream_delta_chunks(self, resp_stream: Iterable[Any]) -> Generator[StreamDelta, None, None]:
        """
        LiteLLMのstreaming Responseからdelta.contentを逐次抽出する。

        注意:
            ここは「ストリームの解釈」だけを担い、ログ出力は行わない。
            ログは、サニタイズ後の本文やUI送信量に合わせたい都合があるため、
            呼び出し側（例: SSE処理）で一元管理する。
        """
        # --- ストリームの各チャンクから差分テキストとfinish_reasonを拾う ---
        for chunk in resp_stream:
            delta_text = _delta_content(chunk)
            finish_reason = _finish_reason(chunk) or None
            if not delta_text and not finish_reason:
                continue
            yield StreamDelta(text=delta_text, finish_reason=finish_reason)
