"""Lightweight language helpers for Doc Studio prompts."""

import re

_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def guess_language(text: str) -> str:
    """Infer language from the input text.

    Args:
        text (str): Input text.

    Returns:
        str: "zh" when CJK characters are detected, otherwise "en".
    """

    return "zh" if _CJK_PATTERN.search(text or "") else "en"
