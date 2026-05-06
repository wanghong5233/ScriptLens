"""Helpers for filtering internal DeepResearch artifacts from chat history."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from models.message import Message
from utils.get_logger import logger

_INTERNAL_SOURCE_MARKERS = {
    "deep_research_internal",
    "deep_research_planner",
    "deep_research_plan_preview",
}

_DEEP_RESEARCH_PROMPT_MARKERS = (
    "你是一名研究规划助手",
    "输出 JSON 数组，每项包含 title, question, depth, parent_title",
    "depth=1 的 parent_title 为空",
    "你的输出不是有效 JSON",
    "请修复并仅输出 JSON 数组",
    "you are a research planner",
    "return a json array; each item has title, question, depth, parent_title",
    "depth=1 parent_title is null",
    "your previous output is invalid json",
    "fix it and output json array only",
)


def _normalize_text(value: Any) -> str:
    """Normalize text for robust heuristic matching."""

    return " ".join(str(value or "").split()).strip()


def _parse_retrieval_payload(raw: Any) -> dict[str, Any]:
    """Parse retrieval payload into a dictionary when possible."""

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def looks_like_deep_research_internal_prompt(raw: str) -> bool:
    """Return True if text looks like an internal DeepResearch planner prompt."""

    text = _normalize_text(raw).lower()
    if not text:
        return False
    return any(marker in text for marker in _DEEP_RESEARCH_PROMPT_MARKERS)


def looks_like_deep_research_plan_payload(raw: str) -> bool:
    """Return True if text looks like planner JSON payload."""

    text = _normalize_text(raw)
    if not text:
        return False
    if not (text.startswith("[") or text.startswith("{")):
        return False
    try:
        payload = json.loads(text)
    except Exception:
        return False
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload.get("items")
    else:
        return False
    if not items:
        return False
    sample = items[:8]
    for item in sample:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("title"), str):
            return False
        if not isinstance(item.get("question"), str):
            return False
        depth = item.get("depth")
        if not isinstance(depth, (int, float, str)):
            return False
        if "parent_title" not in item and "parentTitle" not in item:
            return False
    return True


def is_internal_deep_research_artifact(
    *,
    user_question: Optional[str],
    model_answer: Optional[str],
    retrieval_content: Any = None,
) -> bool:
    """Return True if a chat message is an internal DeepResearch artifact."""

    question = _normalize_text(user_question)
    answer = _normalize_text(model_answer)
    retrieval_payload = _parse_retrieval_payload(retrieval_content)
    source = _normalize_text(retrieval_payload.get("source")).lower()
    if source in _INTERNAL_SOURCE_MARKERS:
        return True
    question_is_internal = looks_like_deep_research_internal_prompt(question)
    answer_is_plan_payload = looks_like_deep_research_plan_payload(answer)
    if question_is_internal and answer_is_plan_payload:
        return True
    if answer_is_plan_payload and (
        "parent_title" in question.lower()
        or "json" in question.lower()
        or "规划" in question
        or "planner" in question.lower()
    ):
        return True
    return False


def purge_internal_deep_research_artifacts(
    db: Session,
    *,
    session_id: str,
) -> int:
    """Delete internal DeepResearch artifact rows from chat messages."""

    try:
        rows = (
            db.query(Message)
            .filter(Message.session_id == session_id)
            .order_by(Message.create_time.asc())
            .all()
        )
        stale_ids = [
            row.message_id
            for row in rows
            if is_internal_deep_research_artifact(
                user_question=row.user_question,
                model_answer=row.model_answer,
                retrieval_content=row.retrieval_content,
            )
            and row.message_id is not None
        ]
        if not stale_ids:
            return 0
        (
            db.query(Message)
            .filter(Message.message_id.in_(stale_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        logger.info(
            "Purged internal DeepResearch artifacts: session=%s count=%s",
            session_id,
            len(stale_ids),
        )
        return len(stale_ids)
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "Failed to purge internal DeepResearch artifacts: session=%s error=%s",
            session_id,
            exc,
        )
        return 0
