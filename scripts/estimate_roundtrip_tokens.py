"""
scripts.estimate_roundtrip_tokens

Gemini / LiteLLM の「1往復あたりの増分トークン」をローカルで概算するCLI。

背景:
    - Gemini は「コンテキスト1M」などの上限があるが、上限はターン数ではなく入力総トークン数で決まる。
    - 1ターンあたりの token 増分を把握できると、何往復できるかを現実的に見積もれる。

方針:
    - `litellm.token_counter()` を使い、ネットワーク無しで token を数える。
    - 「1往復の増分」は `system+user` と `system+user+assistant` の差分として扱う。
      （次ターンの入力履歴に assistant が入るため）
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import litellm


@dataclass(frozen=True)
class RoundtripTokenEstimate:
    """
    1往復分のトークン見積り結果。

    Attributes:
        model: 対象モデル名（LiteLLMの model 指定）。
        prompt_tokens_system_user: system+user の token 数。
        prompt_tokens_system_user_assistant: system+user+assistant の token 数（= 次ターンの入力に近い）。
        delta_tokens_assistant_as_history: assistant 分として増えた token 数（差分）。
        user_chars: user テキストの文字数。
        assistant_chars: assistant テキストの文字数。
        system_chars: system テキストの文字数。
    """

    model: str
    prompt_tokens_system_user: int
    prompt_tokens_system_user_assistant: int
    delta_tokens_assistant_as_history: int
    user_chars: int
    assistant_chars: int
    system_chars: int

    def to_dict(self) -> Dict[str, Any]:
        """ログ/表示向けに辞書化する。"""
        return {
            "model": self.model,
            "prompt_tokens(system+user)": self.prompt_tokens_system_user,
            "prompt_tokens(system+user+assistant)": self.prompt_tokens_system_user_assistant,
            "delta_tokens(assistant_as_history)": self.delta_tokens_assistant_as_history,
            "chars": {
                "system": self.system_chars,
                "user": self.user_chars,
                "assistant": self.assistant_chars,
            },
        }


def _make_repeated_text(chars: int, *, seed: str) -> str:
    """
    指定文字数のテキストを作る。

    NOTE:
        - 「あ」連打のような極端な文字列は tokenizer によって偏ることがあるため、
          ある程度自然な日本語の seed を繰り返して作る。
    """
    # --- 入力正規化 ---
    n = max(0, int(chars))
    s = str(seed or "")
    if n == 0:
        return ""
    if not s:
        # --- seed が空なら、最小限の日本語を使う ---
        s = "こんにちは。"

    # --- 繰り返して必要量を作る ---
    out = (s * ((n // len(s)) + 2))[:n]
    return out


def _build_messages(*, system: str, user: str, assistant: Optional[str] = None) -> List[Dict[str, str]]:
    """
    OpenAI互換 messages を構築する。
    """
    # --- system は空でも role としては入れる（現実の運用に合わせやすい） ---
    messages: List[Dict[str, str]] = [{"role": "system", "content": str(system or "")}]
    messages.append({"role": "user", "content": str(user or "")})
    if assistant is not None:
        messages.append({"role": "assistant", "content": str(assistant or "")})
    return messages


def estimate_roundtrip_tokens(
    *,
    model: str,
    system_text: str,
    user_text: str,
    assistant_text: str,
) -> RoundtripTokenEstimate:
    """
    1往復の token 増分を見積もる。

    Args:
        model: LiteLLM の model 名。
        system_text: system role の本文。
        user_text: user role の本文。
        assistant_text: assistant role の本文（次ターンで履歴として入力に載る想定）。

    Returns:
        RoundtripTokenEstimate: 見積り結果。
    """
    # --- messages 構築 ---
    m_su = _build_messages(system=system_text, user=user_text, assistant=None)
    m_sua = _build_messages(system=system_text, user=user_text, assistant=assistant_text)

    # --- token を数える（ネットワーク不要） ---
    t_su = int(litellm.token_counter(model=str(model), messages=m_su))
    t_sua = int(litellm.token_counter(model=str(model), messages=m_sua))

    # --- 差分は assistant の「履歴としてのコスト」 ---
    delta = max(0, t_sua - t_su)

    return RoundtripTokenEstimate(
        model=str(model),
        prompt_tokens_system_user=t_su,
        prompt_tokens_system_user_assistant=t_sua,
        delta_tokens_assistant_as_history=delta,
        user_chars=len(str(user_text or "")),
        assistant_chars=len(str(assistant_text or "")),
        system_chars=len(str(system_text or "")),
    )


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """
    CLI 引数をパースする。
    """
    p = argparse.ArgumentParser(
        description="Estimate prompt token growth per 1 roundtrip using litellm.token_counter() (offline)."
    )

    # --- モデル ---
    p.add_argument(
        "--model",
        default="google/gemini-3-flash-preview",
        help="LiteLLM model name (e.g. google/gemini-3-flash-preview).",
    )

    # --- system ---
    p.add_argument("--system-text", default="", help="System message content.")
    p.add_argument("--system-chars", type=int, default=None, help="Generate system text by character count.")

    # --- user ---
    p.add_argument("--user-text", default=None, help="User message content.")
    p.add_argument("--user-chars", type=int, default=300, help="Generate user text by character count.")

    # --- assistant ---
    p.add_argument("--assistant-text", default=None, help="Assistant message content.")
    p.add_argument("--assistant-chars", type=int, default=300, help="Generate assistant text by character count.")

    # --- 出力形式 ---
    p.add_argument("--json", action="store_true", help="Output JSON.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """
    CLI エントリポイント。
    """
    args = _parse_args(argv)

    # --- 入力テキストを決定する（明示テキストが最優先） ---
    system_text = str(args.system_text or "")
    if args.system_chars is not None:
        system_text = _make_repeated_text(int(args.system_chars), seed="あなたは役に立つアシスタントです。")

    user_text = str(args.user_text or "")
    if args.user_text is None:
        user_text = _make_repeated_text(int(args.user_chars), seed="今日は何をしようかな。相談に乗って。")

    assistant_text = str(args.assistant_text or "")
    if args.assistant_text is None:
        assistant_text = _make_repeated_text(int(args.assistant_chars), seed="了解。状況をもう少し教えて。")

    # --- 見積もり ---
    est = estimate_roundtrip_tokens(
        model=str(args.model),
        system_text=system_text,
        user_text=user_text,
        assistant_text=assistant_text,
    )

    # --- 出力 ---
    if bool(args.json):
        print(json.dumps(est.to_dict(), ensure_ascii=False, indent=2))
    else:
        d = est.to_dict()
        print(f"model: {d['model']}")
        print(f"prompt_tokens(system+user): {d['prompt_tokens(system+user)']}")
        print(f"prompt_tokens(system+user+assistant): {d['prompt_tokens(system+user+assistant)']}")
        print(f"delta_tokens(assistant_as_history): {d['delta_tokens(assistant_as_history)']}")
        print(f"chars(system/user/assistant): {d['chars']['system']}/{d['chars']['user']}/{d['chars']['assistant']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
