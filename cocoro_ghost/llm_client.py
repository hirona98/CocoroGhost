"""LiteLLM ラッパー。"""

from __future__ import annotations

from typing import Dict, List, Optional

import litellm
import logging


class LlmClient:
    def __init__(self, model: str, reflection_model: str, embedding_model: str, image_model: str, image_timeout_seconds: int = 60, api_key: Optional[str] = None):
        self.logger = logging.getLogger(__name__)
        self.model = model
        self.reflection_model = reflection_model
        self.embedding_model = embedding_model
        self.image_model = image_model
        self.image_timeout_seconds = image_timeout_seconds
        self.api_key = api_key

    def generate_reply(self, system_prompt: str, conversation: List[Dict[str, str]], temperature: float = 0.7) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})
        self.logger.info("LLM reply", extra={"model": self.model})
        resp = litellm.completion(model=self.model, messages=messages, temperature=temperature, api_key=self.api_key)
        return resp["choices"][0]["message"]["content"]

    def generate_reflection(self, system_prompt: str, context_text: str, image_descriptions: Optional[List[str]] = None) -> dict:
        context_block = context_text
        if image_descriptions:
            context_block += "\n" + "\n".join(image_descriptions)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context_block},
        ]
        self.logger.info("LLM reflection", extra={"model": self.reflection_model})
        resp = litellm.completion(model=self.reflection_model, messages=messages, temperature=0.1, api_key=self.api_key)
        return resp["choices"][0]["message"]["content"]

    def generate_embedding(self, texts: List[str], images: Optional[List[bytes]] = None) -> List[List[float]]:
        self.logger.info("LLM embedding", extra={"model": self.embedding_model, "count": len(texts)})
        resp = litellm.embedding(model=self.embedding_model, input=texts, api_key=self.api_key)
        return [item["embedding"] for item in resp["data"]]

    def generate_image_summary(self, images: List[bytes]) -> List[str]:
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
            resp = litellm.completion(
                model=self.image_model,
                messages=messages,
                timeout=self.image_timeout_seconds,
                api_key=self.api_key,
            )
            summaries.append(resp["choices"][0]["message"]["content"])
        return summaries
