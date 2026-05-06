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
    lines = ["【用户既往反馈（最近 N 条，时间倒序）】"]
    for it in items:
        scope = it.get("scope") or "general"
        ref = it.get("scope_ref")
        ref_str = f"@{ref}" if ref else ""
        msg = (it.get("message") or "").strip().replace("\n", " ")
        if len(msg) > 280:
            msg = msg[:280] + "..."
        lines.append(f"- [{scope}{ref_str}] {msg}")
    return "\n".join(lines)
