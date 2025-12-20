"""LiteLLM ラッパー（Response API 対応）。"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, Generator, Iterable, List, Optional

import litellm


def _truncate_for_log(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _response_to_dict(resp: Any) -> Dict[str, Any]:
    """LiteLLM Response をシリアライズ可能な dict に変換。"""
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
    """choices[0].message.content を取り出すユーティリティ。"""
    try:
        choice = resp.choices[0]
        message = getattr(choice, "message", None) or choice["message"]
        content = getattr(message, "content", None) or message["content"]
        # OpenAI 互換で content が list の場合もあるため統一
        if isinstance(content, list):
            return "".join([item.get("text", "") if isinstance(item, dict) else str(item) for item in content]) or ""
        return content or ""
    except Exception:  # noqa: BLE001
        return ""


def _delta_content(resp: Any) -> str:
    """stream chunk から delta.content を取り出す。"""
    try:
        choice = resp.choices[0]
        delta = getattr(choice, "delta", None) or choice.get("delta")
        if not delta:
            return ""
        content = getattr(delta, "content", None) or delta.get("content")
        if content is None:
            return ""
        # OpenAI 互換で content が list の場合もあるため統一
        if isinstance(content, list):
            return "".join([item.get("text", "") if isinstance(item, dict) else str(item) for item in content])
        return str(content)
    except Exception:  # noqa: BLE001
        return ""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """```json ... ``` のようなフェンスがあれば中身だけを取り出す。"""
    if not text:
        return ""
    m = _JSON_FENCE_RE.search(text)
    return m.group(1) if m else text


def _extract_first_json_value(text: str) -> str:
    """文字列から最初の JSON 値（object/array）らしき部分を抜き出す。"""
    text = _strip_code_fences(text).strip()
    if not text:
        return ""

    obj_i = text.find("{")
    arr_i = text.find("[")
    if obj_i == -1 and arr_i == -1:
        return text

    if obj_i == -1:
        start = arr_i
        open_ch, close_ch = "[", "]"
    elif arr_i == -1:
        start = obj_i
        open_ch, close_ch = "{", "}"
    else:
        start = obj_i if obj_i < arr_i else arr_i
        open_ch, close_ch = ("{", "}") if start == obj_i else ("[", "]")

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
    """JSON 文字列内に混入した生の改行/タブ等をエスケープする。"""
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
    """LLM が生成しがちな「ほぼ JSON」を、最低限パースできるように整形する。"""
    if not text:
        return ""
    s = text.strip()
    # スマートクォート対策
    s = s.replace("“", '"').replace("”", '"')
    # 文字列内の改行/タブ等
    s = _escape_control_chars_in_json_strings(s)
    # 末尾カンマ
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _finish_reason(resp: Any) -> str:
    try:
        choice = resp.choices[0]
        finish_reason = getattr(choice, "finish_reason", None) or choice.get("finish_reason")
        return str(finish_reason or "")
    except Exception:  # noqa: BLE001
        return ""


class LlmClient:
    """LLM APIクライアント。"""

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
        self.logger = logging.getLogger(__name__)
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
        """completion API呼び出し用のkwargsを構築。"""
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "return_response_object": True,
            "stream": stream,
        }

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

        # DEBUGログ出力（api_keyはマスク）
        if self.logger.isEnabledFor(logging.DEBUG):
            debug_kwargs = {k: v for k, v in kwargs.items() if k != "api_key"}
            if "api_key" in kwargs:
                debug_kwargs["api_key"] = "***"
            self.logger.debug("LLM request: %s", debug_kwargs)

        return kwargs

    def _log_received(self, *, kind: str, content: str, stream: bool, resp: Any | None = None) -> None:
        if content is None:
            content = ""

        if content:
            preview = _truncate_for_log(content.replace("\r", "").replace("\n", "\\n"), self._INFO_PREVIEW_CHARS)
            self.logger.info("LLM %s received (%s, %d chars): %s", kind, "stream" if stream else "single", len(content), preview)
        else:
            self.logger.info("LLM %s received (%s, empty)", kind, "stream" if stream else "single")

        if self.logger.isEnabledFor(logging.DEBUG):
            debug_text = _truncate_for_log(content, self._DEBUG_PREVIEW_CHARS)
            self.logger.debug("LLM %s content: %s", kind, debug_text)
            if resp is not None:
                self.logger.debug("LLM %s raw response: %s", kind, _response_to_dict(resp))

    def generate_reply_response(
        self,
        system_prompt: str,
        conversation: List[Dict[str, str]],
        temperature: float = 0.7,
        stream: bool = False,
    ):
        """会話応答を生成（Response オブジェクト or ストリーム）。"""
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})

        self.logger.info("LLM reply", extra={"model": self.model, "stream": stream})

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            temperature=temperature,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            stream=stream,
        )

        resp = litellm.completion(**kwargs)
        if not stream:
            self._log_received(kind="reply", content=_first_choice_content(resp), stream=False, resp=resp)
        return resp

    def generate_reflection_response(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
    ):
        """内的思考（リフレクション）を生成（Response オブジェクト）。"""
        context_block = context_text
        if image_descriptions:
            context_block += "\n" + "\n".join(image_descriptions)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context_block},
        ]

        self.logger.info("LLM reflection", extra={"model": self.model})

        kwargs = self._build_completion_kwargs(
            model=self.model,  # reflection_modelは廃止、modelを使用
            messages=messages,
            temperature=0.1,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )

        return litellm.completion(**kwargs)

    def generate_json_response(
        self,
        *,
        system_prompt: str,
        user_text: str,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ):
        """JSON（json_object）を生成（Response オブジェクト）。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

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

        resp = litellm.completion(**kwargs)
        content = _first_choice_content(resp)
        self._log_received(kind="json", content=content, stream=False, resp=resp)

        return resp

    def generate_embedding(
        self,
        texts: List[str],
        images: Optional[List[bytes]] = None,
    ) -> List[List[float]]:
        """テキストの埋め込みベクトルを生成。"""
        self.logger.info(
            "LLM embedding",
            extra={"model": self.embedding_model, "count": len(texts)},
        )

        kwargs = {
            "model": self.embedding_model,
            "input": texts,
        }
        if self.embedding_api_key:
            kwargs["api_key"] = self.embedding_api_key
        if self.embedding_base_url:
            kwargs["api_base"] = self.embedding_base_url

        # DEBUGログ出力（api_keyはマスク）
        if self.logger.isEnabledFor(logging.DEBUG):
            debug_kwargs = {k: v for k, v in kwargs.items() if k != "api_key"}
            if "api_key" in kwargs:
                debug_kwargs["api_key"] = "***"
            self.logger.debug("LLM embedding request: %s", debug_kwargs)

        resp = litellm.embedding(**kwargs)
        try:
            return [item["embedding"] for item in resp["data"]]
        except Exception:  # noqa: BLE001
            return [item.embedding for item in resp.data]

    def generate_image_summary(self, images: List[bytes]) -> List[str]:
        """画像の要約を生成。"""
        summaries: List[str] = []
        for image_bytes in images:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            messages = [
                {"role": "system", "content": "あなたは画像を短い日本語で要約します。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "画像を短く日本語で要約してください。"},
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

            resp = litellm.completion(**kwargs)
            summaries.append(_first_choice_content(resp))

        return summaries

    def response_to_dict(self, resp: Any) -> Dict[str, Any]:
        """Responseオブジェクトをログ/デバッグ用のdictに変換する。"""
        return _response_to_dict(resp)

    def response_content(self, resp: Any) -> str:
        """Responseから本文（choices[0].message.content）を取り出す。"""
        return _first_choice_content(resp)

    def response_json(self, resp: Any) -> Any:
        """LLM 応答本文から JSON を抽出・修復しつつ parse する。"""
        content = self.response_content(resp)
        candidate = _extract_first_json_value(content)
        if not candidate:
            raise ValueError("empty LLM content for JSON parsing")

        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc1:
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
                # 失敗時はデバッグ用に内容を残す（本番ログは INFO で抑制）
                self.logger.debug("response_json parse failed: %s", exc)
                self.logger.debug("response_json content (raw): %s", _truncate_for_log(content, self._DEBUG_PREVIEW_CHARS))
                self.logger.debug("response_json candidate: %s", _truncate_for_log(candidate, self._DEBUG_PREVIEW_CHARS))
                self.logger.debug("response_json repaired: %s", _truncate_for_log(repaired, self._DEBUG_PREVIEW_CHARS))
                raise

    def stream_delta_chunks(self, resp_stream: Iterable[Any]) -> Generator[str, None, None]:
        """LiteLLM の streaming Response から delta.content を逐次抽出。"""
        parts: List[str] = []
        try:
            for chunk in resp_stream:
                delta = _delta_content(chunk)
                if delta:
                    parts.append(delta)
                    yield delta
        finally:
            content = "".join(parts)
            if content:
                self._log_received(kind="reply", content=content, stream=True)

    # 既存コード互換のためのラッパー（文字列を返す）
    def generate_reflection(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
    ) -> str:
        """reflection用のJSON応答を生成し、本文文字列だけ返す（互換API）。"""
        resp = self.generate_reflection_response(system_prompt, context_text, image_descriptions)
        return self.response_content(resp)
