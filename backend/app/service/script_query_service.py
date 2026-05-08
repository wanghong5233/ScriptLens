"""ScriptLens 查询服务（只读）。

把 router 跟原生 SQL 隔离开；router 只调本模块函数，不写 raw SQL。
对应 schema scriptlens 下的 scripts / scenes / reports 三张读路径表。

权限模型：所有查询都强制 user_id 过滤，避免越权读取他人剧本。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


class ScriptNotFoundError(LookupError):
    """剧本不存在或不属于当前用户。"""


def list_user_scripts(*, user_id: int, limit: int = 50, engine: Engine = default_engine) -> List[dict]:
    """GET /api/scripts —— 当前用户的剧本列表（按创建时间倒序）。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, title, status,
                       total_episodes, total_scenes, created_at
                FROM scriptlens.scripts
                WHERE user_id = :uid
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        ).mappings().all()
    return [dict(r) for r in rows]


def get_script_detail(*, script_id: str, user_id: int, engine: Engine = default_engine) -> dict:
    """GET /api/scripts/{id} —— 单一剧本详情。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, title, source_format, status,
                       total_episodes, total_scenes, total_chars,
                       failure_reason, created_at, updated_at
                FROM scriptlens.scripts
                WHERE id = :sid AND user_id = :uid
                """
            ),
            {"sid": script_id, "uid": user_id},
        ).mappings().first()
    if not row:
        raise ScriptNotFoundError(script_id)
    return dict(row)


def get_script_status(*, script_id: str, user_id: int, engine: Engine = default_engine) -> tuple[str, Optional[str]]:
    """轻量查 status / failure_reason，给 GET /report 走 not-ready 分支用。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT status, failure_reason
                FROM scriptlens.scripts
                WHERE id = :sid AND user_id = :uid
                """
            ),
            {"sid": script_id, "uid": user_id},
        ).first()
    if not row:
        raise ScriptNotFoundError(script_id)
    return row.status, row.failure_reason


def get_report(*, script_id: str, user_id: int, engine: Engine = default_engine) -> Optional[tuple[dict, datetime]]:
    """GET /api/scripts/{id}/report —— 已生成则返回 (report_json, generated_at)，否则 None。

    权限验证由 scripts.user_id 把关；reports 表无 user_id 列。
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT r.report_json AS report_json, r.generated_at AS generated_at
                FROM scriptlens.reports r
                JOIN scriptlens.scripts s ON s.id = r.script_id
                WHERE r.script_id = :sid AND s.user_id = :uid
                """
            ),
            {"sid": script_id, "uid": user_id},
        ).first()
    if not row:
        return None
    payload: Any = row.report_json
    if not isinstance(payload, dict):
        # SQLAlchemy 对 jsonb 通常已 deserialize；防御性处理
        import json as _json
        payload = _json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)
    return payload, row.generated_at


def list_scenes(
    *,
    script_id: str,
    user_id: int,
    limit: int = 500,
    engine: Engine = default_engine,
) -> List[dict]:
    """GET /api/scripts/{id}/scenes —— 列出全部场景，前端编辑器渲染用。"""
    # 权限校验
    get_script_status(script_id=script_id, user_id=user_id, engine=engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                LIMIT :limit
                """
            ),
            {"sid": script_id, "limit": limit},
        ).mappings().all()
    return [dict(r) for r in rows]


def update_scene_text(
    *,
    script_id: str,
    scene_id: str,
    user_id: int,
    content: str,
    engine: Engine = default_engine,
) -> dict:
    """PUT /api/scripts/{id}/scenes/{scene_id}/content —— 写回场景全文。

    与 LaTeX accept/reject 路径对称：用户在 AgentDiffReview 里 reject 一个 hunk
    时，前端在内存里算出 reverted 内容，调 updateFileContent(path=scene_id) →
    本端点直接 UPDATE scriptlens.scenes.text。

    权限：先按 (script_id, user_id) 校验剧本归属（统一走 ScriptNotFoundError）。

    Returns:
        dict {"scene_id": str, "char_count": int}
    """
    if not isinstance(content, str):
        raise ValueError("content must be a string")

    # 权限：剧本属于当前用户（间接也校验剧本存在）
    get_script_status(script_id=script_id, user_id=user_id, engine=engine)

    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE scriptlens.scenes
                   SET text = :txt
                 WHERE id = :sid
                   AND script_id = :script_id
                """
            ),
            {"txt": content, "sid": scene_id, "script_id": script_id},
        )
        if result.rowcount == 0:
            raise ScriptNotFoundError(f"scene {scene_id} not found in script {script_id}")

    return {"scene_id": scene_id, "char_count": len(content)}
