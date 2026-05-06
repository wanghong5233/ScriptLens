from __future__ import annotations
import json
import os
import time
from typing import Any, Dict, List, Optional


class AskEventLogger:
    """Append-only JSONL logger for /ask observability.

    Each log line is a JSON object with keys like:
    { ts, user_id, session_id, kb_id, question, top_k, strategy, hits, chunks, citations, usage, answer_chars, variant }
    """

    def __init__(self, log_dir: str = "/app/logs", file_prefix: str = "ask_events") -> None:
        self.log_dir = log_dir
        self.file_prefix = file_prefix
        os.makedirs(self.log_dir, exist_ok=True)

    def _file_path(self) -> str:
        day = time.strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"{self.file_prefix}.{day}.jsonl")

    def log_event(self, data: Dict[str, Any]) -> None:
        try:
            data = dict(data or {})
            data.setdefault("ts", int(time.time() * 1000))
            # 统一截断以防日志过大
            if isinstance(data.get("question"), str) and len(data["question"]) > 2000:
                data["question"] = data["question"][:2000]
            ans = data.get("answer")
            if isinstance(ans, str) and len(ans) > 4000:
                data["answer"] = ans[:4000]
            fp = self._file_path()
            with open(fp, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            # Best-effort logging, never raise
            pass


