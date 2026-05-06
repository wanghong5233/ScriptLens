from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from core.config import settings
from models.message import Message
from service.core.conversation.internal_history_filter import (
    is_internal_deep_research_artifact,
)
from service.core.rag.nlp.model import generate_embedding


@dataclass
class ShortTermMemoryDebug:
    total_messages: int
    scan_limit: int
    selected: int
    summarized: int
    query_type: str
    weights: Dict[str, float]
    avg_score: float
    details: List[Dict[str, object]]


class ShortTermMemoryBuilder:
    """负责构建 STM 记忆切片（按相关性/时间动态挑选历史轮次）。"""

    _QUERY_KEYWORDS = {
        "definition": ["什么是", "定义", "concept", "解释", "meaning"],
        "location": ["刚才", "之前", "上面", "提到", "where", "which page", "上一页"],
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.logger = logging.getLogger("rag.history.stm")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_history(
        self,
        *,
        session_id: str,
        question: str,
        enable_semantic: bool = True,
    ) -> Tuple[List[Dict[str, str]], ShortTermMemoryDebug, Optional[List[float]]]:
        """返回可注入到 LLM 的 history 列表、调试信息以及 query embedding。"""

        scan_limit = max(int(getattr(settings, "SM_STM_SCAN_MESSAGES", 40) or 40), 1)
        query_type = self._classify_query(question or "")
        semantic_weight, time_weight = self._weights_for_query(query_type)

        messages: Sequence[Message] = (
            self.db.query(Message)
            .filter(Message.session_id == session_id)
            .order_by(Message.create_time.desc())
            .limit(scan_limit)
            .all()
        )
        messages = [
            msg
            for msg in messages
            if not is_internal_deep_research_artifact(
                user_question=msg.user_question,
                model_answer=msg.model_answer,
                retrieval_content=msg.retrieval_content,
            )
        ]
        total_messages = len(messages)

        if total_messages == 0:
            debug = ShortTermMemoryDebug(
                total_messages=0,
                scan_limit=scan_limit,
                selected=0,
                summarized=0,
                query_type=query_type,
                weights={"semantic": semantic_weight, "time": time_weight},
                avg_score=0.0,
                details=[],
            )
            try:
                self.logger.debug(
                    "STM.build_history session=%s query_type=%s (empty history)",
                    session_id,
                    query_type,
                )
            except Exception:
                pass
            return [], debug, self._compute_query_embedding(question)

        query_embedding = self._compute_query_embedding(question) if enable_semantic else None

        lambda_decay = float(getattr(settings, "SM_STM_SCORE_DECAY_LAMBDA", 0.1) or 0.1)
        summary_threshold = float(getattr(settings, "SM_STM_SCORE_SUMMARY_THRESHOLD", 0.4) or 0.4)
        keep_threshold = float(getattr(settings, "SM_STM_SCORE_FULL_THRESHOLD", 0.6) or 0.6)
        max_selected = max(int(getattr(settings, "SM_STM_MAX_SELECTED", 6) or 6), 1)
        long_msg_threshold = max(int(getattr(settings, "SM_STM_LONG_MSG_THRESHOLD", 200) or 200), 50)

        now = datetime.now(timezone.utc)
        scored: List[Tuple[Message, float, float, float]] = []  # (message, score, semantic, time)

        for msg in messages:
            msg_time = msg.create_time
            if msg_time is None:
                msg_time = datetime.now(timezone.utc)
            elif msg_time.tzinfo is None:
                msg_time = msg_time.replace(tzinfo=timezone.utc)
            delta_hours = max((now - msg_time).total_seconds() / 3600.0, 0.0)
            time_score = math.exp(-lambda_decay * delta_hours)

            semantic_score = 0.0
            embedding = self._ensure_message_embedding(msg)
            if query_embedding and embedding:
                semantic_score = max(self._cosine_similarity(query_embedding, embedding), 0.0)
            score = semantic_weight * semantic_score + time_weight * time_score
            scored.append((msg, score, semantic_score, time_score))
            if embedding is None and query_embedding is not None:
                # 没有成功生成 embedding，暂不更新
                pass
            elif embedding is None and msg.user_question:
                # embedding 生成失败，已记录日志
                pass
            # NOTE:
            # STM 是读路径，不在这里写回 summary。
            # 之前在此处 flush 会在长流式请求期间持有 messages 行锁，导致会话删除/回卷被阻塞。

        scored.sort(key=lambda item: item[1], reverse=True)
        selected_msgs = scored[:max_selected]
        if selected_msgs:
            selected_msgs.sort(key=lambda item: item[0].create_time or datetime.min)

        history: List[Dict[str, str]] = []
        summarized_count = 0
        for msg, score, _, _ in selected_msgs:
            user_text, assistant_text, summarized = self._prepare_turn_texts(
                msg=msg,
                score=score,
                keep_threshold=keep_threshold,
                summary_threshold=summary_threshold,
                long_msg_threshold=long_msg_threshold,
            )
            summarized_count += 1 if summarized else 0
            if user_text:
                history.append({"role": "user", "content": user_text})
            if assistant_text:
                history.append({"role": "assistant", "content": assistant_text})

        avg_score = sum(item[1] for item in selected_msgs) / max(len(selected_msgs), 1)
        details: List[Dict[str, object]] = []
        for msg, score, semantic_score, time_score in selected_msgs:
            details.append(
                {
                    "message_id": str(getattr(msg, "message_id", "")),
                    "score": round(score, 4),
                    "semantic": round(semantic_score, 4),
                    "time": round(time_score, 4),
                    "summary": bool(msg.user_summary or msg.assistant_summary),
                    "created_at": msg.create_time.isoformat() if msg.create_time else None,
                }
            )

        debug = ShortTermMemoryDebug(
            total_messages=total_messages,
            scan_limit=scan_limit,
            selected=len(selected_msgs),
            summarized=summarized_count,
            query_type=query_type,
            weights={"semantic": semantic_weight, "time": time_weight},
            avg_score=avg_score,
            details=details,
        )

        try:
            self.logger.debug(
                "STM.build_history session=%s total=%s selected=%s summarized=%s max_selected=%s query_type=%s weights=%s",
                session_id,
                total_messages,
                len(selected_msgs),
                summarized_count,
                max_selected,
                query_type,
                {"semantic": semantic_weight, "time": time_weight},
            )
        except Exception:
            pass

        return history, debug, query_embedding

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _classify_query(self, query: str) -> str:
        lowered = (query or "").lower()
        for qtype, keywords in self._QUERY_KEYWORDS.items():
            for kw in keywords:
                if kw and kw.lower() in lowered:
                    return qtype
        return "default"

    def _weights_for_query(self, query_type: str) -> Tuple[float, float]:
        if query_type == "definition":
            return 0.85, 0.15
        if query_type == "location":
            return 0.3, 0.7
        return 0.7, 0.3

    def _compute_query_embedding(self, text: str) -> Optional[List[float]]:
        text = (text or "").strip()
        if not text:
            return None
        try:
            vecs = generate_embedding([text])
            if vecs and vecs[0] is not None:
                return list(vecs[0])
        except Exception as exc:
            self.logger.debug("STM query embedding failed: %s", exc)
        return None

    def _ensure_message_embedding(self, msg: Message) -> Optional[List[float]]:
        if msg.user_embedding is not None:
            return list(msg.user_embedding)
        if not bool(getattr(settings, "SM_STM_EMBED_MISSING_ON_READ", False)):
            return None
        text = (msg.user_question or "").strip()
        if not text:
            return None
        try:
            vecs = generate_embedding([text])
            if vecs and vecs[0] is not None:
                embedding = list(vecs[0])
                msg.user_embedding = embedding
                return embedding
        except Exception as exc:
            self.logger.debug("STM message embedding failed: %s", exc)
        return None

    def _prepare_turn_texts(
        self,
        *,
        msg: Message,
        score: float,
        keep_threshold: float,
        summary_threshold: float,
        long_msg_threshold: int,
    ) -> Tuple[str, str, bool]:
        summarize = score < keep_threshold
        summarized_flag = False

        user_text = msg.user_question or ""
        assistant_text = msg.model_answer or ""

        if summarize and score >= summary_threshold:
            user_text = msg.user_summary or self._build_summary(user_text, long_msg_threshold)
            assistant_text = msg.assistant_summary or self._build_summary(assistant_text, long_msg_threshold)
            summarized_flag = True
        elif summarize and score < summary_threshold:
            user_text = msg.user_summary or self._build_summary(user_text, long_msg_threshold)
            assistant_text = msg.assistant_summary or self._build_summary(assistant_text, long_msg_threshold)
            summarized_flag = True

        return user_text, assistant_text, summarized_flag

    def _build_summary(self, text: str, max_chars: int) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        sentences = re.split(r"(?<=[。！？!?\.])\s*", text)
        summary_parts: List[str] = []
        total = 0
        for sent in sentences:
            s = sent.strip()
            if not s:
                continue
            summary_parts.append(s)
            total += len(s)
            if total >= max_chars:
                break
        if not summary_parts:
            return text[:max_chars]
        summary = " ".join(summary_parts)
        return summary[:max_chars]

    def _cosine_similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b:
            return 0.0
        length = min(len(a), len(b))
        if length == 0:
            return 0.0
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for i in range(length):
            av = float(a[i])
            bv = float(b[i])
            dot += av * bv
            norm_a += av * av
            norm_b += bv * bv
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return dot / math.sqrt(norm_a * norm_b)


