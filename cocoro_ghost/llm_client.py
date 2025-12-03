"""LiteLLM ラッパー。"""

from __future__ import annotations

from typing import Dict, List, Optional

import litellm


class LlmClient:
    def __init__(self, model: str, reflection_model: str, embedding_model: str):
        self.model = model
        self.reflection_model = reflection_model
        self.embedding_model = embedding_model

    def generate_reply(self, system_prompt: str, conversation: List[Dict[str, str]], temperature: float = 0.7) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        for m in conversation:
            messages.append({"role": m["role"], "content": m["content"]})
        resp = litellm.completion(model=self.model, messages=messages, temperature=temperature)
        return resp["choices"][0]["message"]["content"]

    def generate_reflection(self, system_prompt: str, context_text: str, image_descriptions: Optional[List[str]] = None) -> dict:
        context_block = context_text
        if image_descriptions:
            context_block += "\n" + "\n".join(image_descriptions)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context_block},
        ]
        resp = litellm.completion(model=self.reflection_model, messages=messages, temperature=0.1)
        return resp["choices"][0]["message"]["content"]

    def generate_embedding(self, texts: List[str], images: Optional[List[bytes]] = None) -> List[List[float]]:
        resp = litellm.embedding(model=self.embedding_model, input=texts)
        return [item["embedding"] for item in resp["data"]]

    def generate_image_summary(self, images: List[bytes]) -> List[str]:
        # LiteLLM の画像対応はプロバイダ依存のため、ここでは明示的に未サポートとする。
        raise RuntimeError("画像要約は対応プロバイダ設定後に実装してください")
