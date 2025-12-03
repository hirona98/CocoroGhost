"""LiteLLM ラッパー。実装はダミーで、キーが設定されるまでは例外を送出する。"""

from __future__ import annotations

from typing import Dict, List, Optional


class LlmClient:
    def __init__(self, model: str, reflection_model: str, embedding_model: str):
        self.model = model
        self.reflection_model = reflection_model
        self.embedding_model = embedding_model

    def generate_reply(self, system_prompt: str, conversation: List[Dict[str, str]], temperature: float = 0.7) -> str:
        raise RuntimeError("LLM クライアントが未実装です")

    def generate_reflection(self, system_prompt: str, context_text: str, image_descriptions: Optional[List[str]] = None) -> dict:
        raise RuntimeError("LLM クライアントが未実装です")

    def generate_embedding(self, texts: List[str], images: Optional[List[bytes]] = None) -> List[List[float]]:
        raise RuntimeError("LLM クライアントが未実装です")

    def generate_image_summary(self, images: List[bytes]) -> List[str]:
        raise RuntimeError("LLM クライアントが未実装です")
