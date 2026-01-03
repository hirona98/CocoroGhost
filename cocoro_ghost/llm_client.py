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


class LlmRequestPurpose:
    """LLM呼び出しの処理目的（ログ用途のラベル）。"""

    # docs/prompt_usage_map.md のフローに対応した日本語ラベル
    CONVERSATION = "＜＜ 会話応答作成 ＞＞"
    NOTIFICATION = "＜＜ 通知返答 ＞＞"
    META_REQUEST = "＜＜ メタ要求対応 ＞＞"
    INTERNAL_THOUGHT = "＜＜ 内的思考（反射） ＞＞"
    ENTITY_EXTRACT = "＜＜ エンティティ（実体）抽出 ＞＞"
    FACT_EXTRACT = "＜＜ 事実抽出 ＞＞"
    LOOP_EXTRACT = "＜＜ 未完了事項抽出 ＞＞"
    ENTITY_NAME_EXTRACT = "＜＜ エンティティ（実体）名抽出 ＞＞"
    SHARED_NARRATIVE_SUMMARY = "＜＜ 背景共有サマリ生成 ＞＞"
    PERSON_SUMMARY = "＜＜ 人物サマリ生成 ＞＞"
    TOPIC_SUMMARY = "＜＜ トピックサマリ生成 ＞＞"
    RETRIEVAL_QUERY_EMBEDDING = "＜＜ 記憶検索用クエリの埋め込み取得 ＞＞"
    UNIT_EMBEDDING = "＜＜ ユニット埋め込み ＞＞"
    IMAGE_SUMMARY_CHAT = "＜＜ 画像要約（会話） ＞＞"
    IMAGE_SUMMARY_NOTIFICATION = "＜＜ 画像要約（通知） ＞＞"
    IMAGE_SUMMARY_META_REQUEST = "＜＜ 画像要約（メタ要求対応） ＞＞"
    IMAGE_SUMMARY_DESKTOP_WATCH = "＜＜ 画像要約（デスクトップウォッチ） ＞＞"
    VISION_DECISION = "＜＜ 視覚判定（チャット） ＞＞"
    DESKTOP_WATCH = "＜＜ デスクトップウォッチ ＞＞"
    REMINDER = "＜＜ リマインダー ＞＞"


def _normalize_purpose(purpose: str) -> str:
    """空文字などを吸収し、ログに最低限の目的ラベルを残す。"""
    label = str(purpose or "").strip()
    return label or "不明"


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
        max_tokens: int = 4096,
        max_tokens_vision: int = 4096,
        image_timeout_seconds: int = 60,
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
            reasoning_effort: 推論詳細度設定（o1系用）
            max_tokens: 通常時の最大トークン数
            max_tokens_vision: 画像認識時の最大トークン数
            image_timeout_seconds: 画像処理タイムアウト秒数
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
        self.max_tokens = max_tokens
        self.max_tokens_vision = max_tokens_vision
        self.image_timeout_seconds = image_timeout_seconds

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

        # reasoning_effort対応（OpenAI o1系など）
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}

        return kwargs

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

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            stream=stream,
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

    def generate_reflection_response(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
        purpose: str = LlmRequestPurpose.INTERNAL_THOUGHT,
    ):
        """
        内的思考（リフレクション）を生成する（Responseオブジェクト）。
        コンテキストに基づいてJSON形式の思考結果を返す。
        purpose はログに出す処理目的ラベル。
        """
        # コンテキストブロックを構築
        context_block = context_text
        if image_descriptions:
            context_block += "\n" + "\n".join(image_descriptions)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context_block},
        ]

        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)
        start = time.perf_counter()

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )

        if llm_log_level != "OFF":
            self._log_llm_info(
                "LLM request 送信 %s kind=reflection stream=%s messages=%s 文字数=%s",
                purpose_label,
                False,
                len(messages),
                _estimate_text_chars(messages),
            )
        self._log_llm_payload("LLM request (reflection)", _sanitize_for_llm_log(kwargs), llm_log_level=llm_log_level)

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                # コンテキスト長超過は原因を明確に区別する（ERROR）。
                if self._is_context_window_exceeded(exc):
                    self._log_context_window_exceeded_error(
                        purpose_label=purpose_label,
                        kind="reflection",
                        elapsed_ms=elapsed_ms,
                        approx_chars=_estimate_text_chars(messages),
                        messages_count=len(messages),
                        stream=False,
                        exc=exc,
                    )
                else:
                    self._log_llm_error(
                        "LLM request failed %s kind=reflection ms=%s error=%s",
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
                "LLM response 受信 %s kind=reflection finish_reason=%s chars=%s ms=%s",
                purpose_label,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        self._log_llm_payload(
            "LLM response (reflection)",
            _sanitize_for_llm_log({"finish_reason": finish_reason, "content": content}),
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
        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=requested_max_tokens,
            response_format={"type": "json_object"},
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
            model_for_request = _to_openai_compatible_model_for_slug(openrouter_slug)

        kwargs = {
            "model": model_for_request,
            "input": texts,
        }
        if self.embedding_api_key:
            kwargs["api_key"] = self.embedding_api_key
        if api_base_for_request:
            kwargs["api_base"] = api_base_for_request

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

    def generate_image_summary(self, images: List[bytes], purpose: str) -> List[str]:
        """
        画像の要約を生成する。
        各画像をVision LLMで解析し、日本語で説明テキストを返す。
        purpose はログに出す処理目的ラベル。
        """
        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        purpose_label = _normalize_purpose(purpose)
        max_workers = 4

        def _process_one(image_bytes: bytes) -> str:
            start = time.perf_counter()
            if llm_log_level != "OFF":
                self._log_llm_info(
                    "LLM request 送信 %s kind=vision image_bytes=%s",
                    purpose_label,
                    len(image_bytes),
                )
            # 画像をbase64エンコード
            b64 = base64.b64encode(image_bytes).decode("ascii")
            messages = [
                {"role": "system", "content": "あなたは日本語で画像を説明します。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "この画像を日本語で詳細に説明してください。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
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
            return content

        if len(images) <= 1:
            return [_process_one(image_bytes) for image_bytes in images]

        worker_count = min(max_workers, len(images))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return list(executor.map(_process_one, images))

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
        ストリーミング応答をリアルタイムで処理し、finish_reasonも返すジェネレータ。
        """
        # ストリームの各チャンクから差分テキストとfinish_reasonを拾う。
        for chunk in resp_stream:
            delta_text = _delta_content(chunk)
            finish_reason = _finish_reason(chunk) or None
            if not delta_text and not finish_reason:
                continue
            yield StreamDelta(text=delta_text, finish_reason=finish_reason)

    def generate_reflection(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
        purpose: str = LlmRequestPurpose.INTERNAL_THOUGHT,
    ) -> str:
        """
        reflection用のJSON応答を生成し、本文文字列だけ返す（薄いラッパー）。
        generate_reflection_responseの結果から本文のみを抽出する。
        purpose はログに出す処理目的ラベル。
        """
        resp = self.generate_reflection_response(
            system_prompt,
            context_text,
            image_descriptions,
            purpose=purpose,
        )
        return self.response_content(resp)
