"""LiteLLM ラッパー。"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import litellm


class LlmClient:
    """LLM APIクライアント。"""

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
    ) -> Dict:
        """completion API呼び出し用のkwargsを構築。"""
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
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

        return kwargs

    def generate_reply(
        self,
        system_prompt: str,
        conversation: List[Dict[str, str]],
        temperature: float = 0.7,
    ) -> str:
        """会話応答を生成。"""
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})

        self.logger.info("LLM reply", extra={"model": self.model})

        kwargs = self._build_completion_kwargs(
            model=self.model,
            messages=messages,
            temperature=temperature,
            api_key=self.api_key,
            base_url=self.llm_base_url,
            max_tokens=self.max_tokens,
        )

        resp = litellm.completion(**kwargs)
        return resp["choices"][0]["message"]["content"]

    def generate_reflection(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
    ) -> dict:
        """内的思考（リフレクション）を生成。"""
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

        resp = litellm.completion(**kwargs)
        return resp["choices"][0]["message"]["content"]

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

        resp = litellm.embedding(**kwargs)
        return [item["embedding"] for item in resp["data"]]

    def generate_image_summary(self, images: List[bytes]) -> List[str]:
        """画像の要約を生成。"""
        import base64

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
            summaries.append(resp["choices"][0]["message"]["content"])

        return summaries
