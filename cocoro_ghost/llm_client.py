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
from typing import Any, Dict, Generator, Iterable, List, Optional

import litellm

from cocoro_ghost.llm_debug import log_llm_payload, normalize_llm_log_level


def _truncate_for_log(text: str, limit: int) -> str:
    """ログ出力用にテキストを切り詰める。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


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


class LlmClient:
    """
    LLM APIクライアント。
    LiteLLMを使用してLLM APIを呼び出し、会話応答やJSON生成を行う。
    """

    # ログ出力時のプレビュー文字数
    _INFO_PREVIEW_CHARS = 300
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
            llm_base_url: LLM APIベースURL
            embedding_base_url: 埋め込みAPIベースURL
            image_llm_base_url: 画像モデルAPIベースURL
            image_model_api_key: 画像モデルAPIキー
            reasoning_effort: 推論詳細度設定（o1系用）
            max_tokens: 通常時の最大トークン数
            max_tokens_vision: 画像認識時の最大トークン数
            image_timeout_seconds: 画像処理タイムアウト秒数
        """
        self.logger = logging.getLogger(__name__)
        # NOTE: LLM送受信ログは専用ロガーに分離する。
        self.io_logger = logging.getLogger("cocoro_ghost.llm_io")
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

    def _build_completion_kwargs(
        self,
        model: str,
        messages: List[Dict],
        temperature: float,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        timeout: Optional[int] = None,
        stream: bool = False,
    ) -> Dict:
        """
        completion API呼び出し用のkwargsを構築する。
        モデル固有の制約（gpt-5系のtemperature制限等）を考慮する。
        """
        # gpt-5 以降は temperature の制約が厳しい（LiteLLM側で弾かれる）
        # - gpt-5 / gpt-5-mini / gpt-6 ... 等: temperature は 1 のみ
        model_l = (model or "").lower()
        temp = float(temperature)
        m = re.search(r"\bgpt-(\d+)(?:\.(\d+))?\b", model_l)
        if m:
            major = int(m.group(1))
            if major >= 5:
                temp = 1.0

        # 基本パラメータ
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temp,
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
        temperature: float = 0.7,
        stream: bool = False,
    ):
        """
        会話応答を生成する（Responseオブジェクト or ストリーム）。
        システムプロンプトと会話履歴からLLMに応答を生成させる。
        """
        # メッセージ配列を構築
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})

        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        start = time.perf_counter()

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            temperature=temperature,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            stream=stream,
        )

        msg_count = len(messages)
        approx_chars = _estimate_text_chars(messages)
        if llm_log_level != "OFF":
            self.io_logger.info(
                "LLM request sent kind=chat model=%s stream=%s messages=%s approx_chars=%s",
                self.model,
                bool(stream),
                msg_count,
                approx_chars,
            )
        log_llm_payload(
            self.io_logger,
            "LLM request (chat)",
            _sanitize_for_llm_log(kwargs),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.error(
                    "LLM request failed kind=chat model=%s stream=%s messages=%s ms=%s error=%s",
                    self.model,
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
            self.io_logger.info(
                "LLM response received kind=chat model=%s stream=%s finish_reason=%s chars=%s ms=%s",
                self.model,
                False,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        log_llm_payload(
            self.io_logger,
            "LLM response (chat)",
            _sanitize_for_llm_log(
                {
                    "model": self.model,
                    "finish_reason": finish_reason,
                    "content": content,
                }
            ),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )
        return resp

    def generate_reflection_response(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
    ):
        """
        内的思考（リフレクション）を生成する（Responseオブジェクト）。
        コンテキストに基づいてJSON形式の思考結果を返す。
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
        start = time.perf_counter()

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            temperature=0.1,  # リフレクションは低温度で安定性重視
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )

        if llm_log_level != "OFF":
            self.io_logger.info(
                "LLM request sent kind=reflection model=%s stream=%s messages=%s approx_chars=%s",
                self.model,
                False,
                len(messages),
                _estimate_text_chars(messages),
            )
        log_llm_payload(
            self.io_logger,
            "LLM request (reflection)",
            _sanitize_for_llm_log(kwargs),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.error(
                    "LLM request failed kind=reflection model=%s ms=%s error=%s",
                    self.model,
                    elapsed_ms,
                    str(exc),
                    exc_info=exc,
                )
            raise

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = _first_choice_content(resp)
        finish_reason = _finish_reason(resp)
        if llm_log_level != "OFF":
            self.io_logger.info(
                "LLM response received kind=reflection model=%s finish_reason=%s chars=%s ms=%s",
                self.model,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        log_llm_payload(
            self.io_logger,
            "LLM response (reflection)",
            _sanitize_for_llm_log({"finish_reason": finish_reason, "content": content}),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )
        return resp

    def generate_json_response(
        self,
        *,
        system_prompt: str,
        user_text: str,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ):
        """JSON（json_object）を生成する（Responseオブジェクト）。

        INFO: 送受信した事実（メタ情報）のみ
        DEBUG: 内容も出す（マスク＋トリミング）
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        start = time.perf_counter()

        requested_max_tokens = max_tokens or self.max_tokens
        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            temperature=temperature,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=requested_max_tokens,
            response_format={"type": "json_object"},
        )

        if llm_log_level != "OFF":
            self.io_logger.info(
                "LLM request sent kind=json model=%s stream=%s messages=%s approx_chars=%s",
                self.model,
                False,
                len(messages),
                _estimate_text_chars(messages),
            )
        log_llm_payload(
            self.io_logger,
            "LLM request (json)",
            _sanitize_for_llm_log(kwargs),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.error(
                    "LLM request failed kind=json model=%s ms=%s error=%s",
                    self.model,
                    elapsed_ms,
                    str(exc),
                    exc_info=exc,
                )
            raise

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content = _first_choice_content(resp)
        finish_reason = _finish_reason(resp)
        if llm_log_level != "OFF":
            self.io_logger.info(
                "LLM response received kind=json model=%s finish_reason=%s chars=%s ms=%s",
                self.model,
                finish_reason,
                len(content or ""),
                elapsed_ms,
            )
        log_llm_payload(
            self.io_logger,
            "LLM response (json)",
            _sanitize_for_llm_log({"finish_reason": finish_reason, "content": content}),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )
        return resp

    def generate_embedding(
        self,
        texts: List[str],
        images: Optional[List[bytes]] = None,
    ) -> List[List[float]]:
        """
        テキストの埋め込みベクトルを生成する。
        複数テキストを一括処理し、各テキストに対応するベクトルを返す。
        """
        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        start = time.perf_counter()
        if llm_log_level != "OFF":
            self.io_logger.info(
                "LLM request sent kind=embedding model=%s count=%s approx_chars=%s",
                self.embedding_model,
                len(texts),
                sum(len(t or "") for t in texts),
            )
        # NOTE: embedding入力は漏洩しやすいので、DEBUGでもトリミングされる前提で出す。
        log_llm_payload(
            self.io_logger,
            "LLM request (embedding)",
            _sanitize_for_llm_log({"model": self.embedding_model, "input": texts, "count": len(texts)}),
            max_chars=self._DEBUG_PREVIEW_CHARS,
            llm_log_level=llm_log_level,
        )

        kwargs = {
            "model": self.embedding_model,
            "input": texts,
        }
        if self.embedding_api_key:
            kwargs["api_key"] = self.embedding_api_key
        if self.embedding_base_url:
            kwargs["api_base"] = self.embedding_base_url

        try:
            resp = litellm.embedding(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.error(
                    "LLM request failed kind=embedding model=%s count=%s ms=%s error=%s",
                    self.embedding_model,
                    len(texts),
                    elapsed_ms,
                    str(exc),
                    exc_info=exc,
                )
            raise

        # レスポンス形式に応じてベクトルを抽出
        try:
            out = [item["embedding"] for item in resp["data"]]
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.info(
                    "LLM response received kind=embedding model=%s count=%s ms=%s",
                    self.embedding_model,
                    len(out),
                    elapsed_ms,
                )
            return out
        except Exception:  # noqa: BLE001
            out = [item.embedding for item in resp.data]
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.info(
                    "LLM response received kind=embedding model=%s count=%s ms=%s",
                    self.embedding_model,
                    len(out),
                    elapsed_ms,
                )
            return out

    def generate_image_summary(self, images: List[bytes]) -> List[str]:
        """
        画像の要約を生成する。
        各画像をVision LLMで解析し、日本語で説明テキストを返す。
        """
        llm_log_level = normalize_llm_log_level(self._get_llm_log_level())
        summaries: List[str] = []
        for image_bytes in images:
            start = time.perf_counter()
            if llm_log_level != "OFF":
                self.io_logger.info(
                    "LLM request sent kind=vision model=%s image_bytes=%s",
                    self.image_model,
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
                temperature=0.3,
                api_key=self.image_model_api_key,
                base_url=self.image_llm_base_url,
                max_tokens=self.max_tokens_vision,
                timeout=self.image_timeout_seconds,
            )

            log_llm_payload(
                self.io_logger,
                "LLM request (vision)",
                _sanitize_for_llm_log(kwargs),
                max_chars=self._INFO_PREVIEW_CHARS,
                llm_log_level=llm_log_level,
            )

            try:
                resp = litellm.completion(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if llm_log_level != "OFF":
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    self.io_logger.error(
                        "LLM request failed kind=vision model=%s image_bytes=%s ms=%s error=%s",
                        self.image_model,
                        len(image_bytes),
                        elapsed_ms,
                        str(exc),
                        exc_info=exc,
                    )
                raise
            content = _first_choice_content(resp)
            summaries.append(content)
            if llm_log_level != "OFF":
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                self.io_logger.info(
                    "LLM response received kind=vision model=%s chars=%s ms=%s",
                    self.image_model,
                    len(content or ""),
                    elapsed_ms,
                )
            log_llm_payload(
                self.io_logger,
                "LLM response (vision)",
                _sanitize_for_llm_log({"content": content, "finish_reason": _finish_reason(resp)}),
                max_chars=self._DEBUG_PREVIEW_CHARS,
                llm_log_level=llm_log_level,
            )

        return summaries

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
                self.logger.debug("response_json content (raw): %s", _truncate_for_log(content, self._DEBUG_PREVIEW_CHARS))
                self.logger.debug("response_json candidate: %s", _truncate_for_log(candidate, self._DEBUG_PREVIEW_CHARS))
                self.logger.debug("response_json repaired: %s", _truncate_for_log(repaired, self._DEBUG_PREVIEW_CHARS))
                raise

    def stream_delta_chunks(self, resp_stream: Iterable[Any]) -> Generator[str, None, None]:
        """
        LiteLLMのstreaming Responseからdelta.contentを逐次抽出する。
        ストリーミング応答をリアルタイムで処理するジェネレータ。
        """
        parts: List[str] = []
        for chunk in resp_stream:
            delta = _delta_content(chunk)
            if delta:
                parts.append(delta)
                yield delta

    def generate_reflection(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
    ) -> str:
        """
        reflection用のJSON応答を生成し、本文文字列だけ返す（薄いラッパー）。
        generate_reflection_responseの結果から本文のみを抽出する。
        """
        resp = self.generate_reflection_response(system_prompt, context_text, image_descriptions)
        return self.response_content(resp)
