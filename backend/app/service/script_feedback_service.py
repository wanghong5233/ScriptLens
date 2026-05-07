"""ScriptLens 用户反馈服务（PRD §10 P3 轻量 skill 机制）。

写入 `scriptlens.script_feedback` 表；提供 chat 端点取最近 N 条注入 prompt。
故意不做向量化、不做 RL、不做 reward model——只是 prompt 注入。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


_VALID_SCOPES = {"general", "dimension", "rewrite", "scene"}

_SKILL_SLOTS = {
    "dimension_explanation": "维度解释偏好",
    "rewrite_preference": "改写偏好",
    "risk_guardrail": "风险规避偏好",
}

_RISK_KEYWORDS = ("风险", "审核", "合规", "红线", "暴力", "伦理", "价值观", "敏感")
_REWRITE_KEYWORDS = ("改写", "重写", "润色", "语气", "风格", "保留", "避免", "不要")
_DIMENSION_KEYWORDS = ("评分", "维度", "解释", "依据", "证据", "理由")


class FeedbackError(Exception):
    """反馈写入失败（参数非法 / 剧本不存在 / 越权）。"""


def add_feedback(
    *,
    script_id: str,
    user_id: int,
    scope: str,
    message: str,
    scope_ref: Optional[str] = None,
    engine: Engine = default_engine,
) -> Dict[str, Any]:
    """写一条反馈，返回完整记录。

    会先校验 (script_id, user_id) 归属，避免给别人剧本灌反馈。
    """
    if scope not in _VALID_SCOPES:
        raise FeedbackError(f"非法 scope={scope}；可选：{sorted(_VALID_SCOPES)}")
    msg = (message or "").strip()
    if not msg:
        raise FeedbackError("message 不能为空")

    fb_id = str(uuid.uuid4())
    with engine.begin() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM scriptlens.scripts WHERE id = :sid"),
            {"sid": script_id},
        ).scalar()
        if owner is None:
            raise FeedbackError("剧本不存在")
        if int(owner) != int(user_id):
            raise FeedbackError("无权对该剧本提交反馈")

        conn.execute(
            text(
                """
                INSERT INTO scriptlens.script_feedback
                    (id, script_id, user_id, message, scope, scope_ref, created_at)
                VALUES
                    (:id, :sid, :uid, :msg, :scope, :ref, NOW())
                """
            ),
            {
                "id": fb_id,
                "sid": script_id,
                "uid": user_id,
                "msg": msg,
                "scope": scope,
                "ref": scope_ref,
            },
        )

    logger.info(
        "feedback added: script_id=%s user_id=%s scope=%s ref=%s len=%s",
        script_id, user_id, scope, scope_ref, len(msg),
    )
    return {
        "id": fb_id,
        "script_id": script_id,
        "scope": scope,
        "scope_ref": scope_ref,
        "message": msg,
    }


def list_recent_feedback(
    *,
    script_id: str,
    user_id: int,
    limit: int = 10,
    engine: Engine = default_engine,
) -> List[Dict[str, Any]]:
    """读最近 N 条该用户对该剧本的反馈，按时间倒序。

    chat 端点会调它，把内容塞进 system prompt。
    """
    rows = []
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT id::text AS id, scope, scope_ref, message, created_at
                FROM scriptlens.script_feedback
                WHERE script_id = :sid AND user_id = :uid
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            {"sid": script_id, "uid": user_id, "lim": int(limit)},
        ).mappings().all()
        rows = [dict(r) for r in result]
    return rows


def format_feedback_for_prompt(items: List[Dict[str, Any]]) -> str:
    """把反馈列表渲染成 prompt 可注入的多行文本。

    输入空列表返回空字符串（避免在 prompt 头部出现空段落）。
    """
    if not items:
        return ""
    skill_pack = derive_feedback_skills(items)
    lines: List[str] = []
    if any(skill_pack.values()):
        lines.append("【已学习的用户偏好 / Skill 槽】")
        for slot, label in _SKILL_SLOTS.items():
            values = skill_pack.get(slot) or []
            if not values:
                continue
            lines.append(f"{label}：")
            for msg in values:
                lines.append(f"- {msg}")
        lines.append("")

    lines.append("【用户既往反馈（最近 N 条，时间倒序）】")
    for it in items:
        scope = it.get("scope") or "general"
        ref = it.get("scope_ref")
        ref_str = f"@{ref}" if ref else ""
        msg = _sanitize_message(it.get("message"), max_len=280)
        if not msg:
            continue
        lines.append(f"- [{scope}{ref_str}] {msg}")
    return "\n".join(lines)


def derive_feedback_skills(items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """从用户反馈中抽取轻量 skill 槽。

    这是 task.md §五 5 的最小可工作形态：不训练模型、不做复杂调度，只把
    高频反馈固化成 Agent 可读的偏好槽，下一轮 chat 自动生效。
    """
    slots: Dict[str, List[str]] = {key: [] for key in _SKILL_SLOTS}
    seen: set[tuple[str, str]] = set()
    for item in items:
        msg = _sanitize_message(item.get("message"), max_len=160)
        if not msg:
            continue
        slot = _classify_feedback(item.get("scope"), msg)
        key = (slot, msg)
        if key in seen:
            continue
        if len(slots[slot]) >= 4:
            continue
        seen.add(key)
        slots[slot].append(msg)
    return slots


def _classify_feedback(scope: object, message: str) -> str:
    scope_text = str(scope or "")
    if _contains_any(message, _RISK_KEYWORDS):
        return "risk_guardrail"
    if scope_text == "rewrite" or _contains_any(message, _REWRITE_KEYWORDS):
        return "rewrite_preference"
    if scope_text == "dimension" or _contains_any(message, _DIMENSION_KEYWORDS):
        return "dimension_explanation"
    return "dimension_explanation"


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _sanitize_message(value: object, *, max_len: int) -> str:
    msg = str(value or "").strip().replace("\n", " ")
    msg = " ".join(msg.split())
    if len(msg) > max_len:
        msg = msg[:max_len] + "..."
    return msg
