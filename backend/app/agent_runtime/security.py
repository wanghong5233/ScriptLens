"""输入安全与速率限制工具。"""

from __future__ import annotations

import re
import time
from collections import deque, defaultdict
from typing import Deque, Dict, Optional, Tuple


INJECTION_PATTERNS = [
    r"忽略.*之前.*指令",
    r"ignore.*previous.*instruction",
    r"<\|im_start\|>system",
    r"system\s*:",
    r"请忽略.*以下.*限制",
    r"reset\s*role",
]

MAX_INPUT_LENGTH = 2000


def sanitize_user_input(user_input: str) -> Tuple[str, Optional[str]]:
    """清洗用户输入，返回 (clean_text, warning)。"""
    text = user_input.strip()
    if not text:
        return text, None

    warning = None

    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]
        warning = f"输入已被截断至 {MAX_INPUT_LENGTH} 字符。"

    lowered = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            raise ValueError("检测到潜在的 Prompt Injection，请重新描述您的需求。")

    return text, warning


class UserRateLimiter:
    """简单的基于内存的速率限制。"""

    def __init__(self, per_minute: int = 10, per_hour: int = 100) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._requests: Dict[int, Deque[float]] = defaultdict(deque)

    def check(self, user_id: int) -> None:
        now = time.time()
        minute_ago = now - 60
        hour_ago = now - 3600
        queue = self._requests[user_id]

        while queue and queue[0] < hour_ago:
            queue.popleft()

        recent_minute = [ts for ts in queue if ts >= minute_ago]
        if len(recent_minute) >= self.per_minute:
            raise ValueError("请求过于频繁，请稍后再试。")

        if len(queue) >= self.per_hour:
            raise ValueError("已达到每小时请求上限，请稍后再试。")

        queue.append(now)


