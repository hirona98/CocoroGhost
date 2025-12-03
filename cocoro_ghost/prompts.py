"""プロンプト管理（簡易版）。"""


CHARACTER_SYSTEM_PROMPT = """あなたは CocoroAI という一人のユーザー専用のパートナーAIです。"""

REFLECTION_SYSTEM_PROMPT = """あなたは cocoro_ghost の reflection モジュールとして内的思考をJSONで返します。"""

NOTIFICATION_SYSTEM_PROMPT = """通知内容を要約し、ユーザーに日本語で伝えます。"""


def get_character_prompt() -> str:
    return CHARACTER_SYSTEM_PROMPT


def get_reflection_prompt() -> str:
    return REFLECTION_SYSTEM_PROMPT


def get_notification_prompt() -> str:
    return NOTIFICATION_SYSTEM_PROMPT
