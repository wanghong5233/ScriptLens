"""ScriptLens 剧本删除服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from service.script_query_service import ScriptNotFoundError
from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


@dataclass
class ScriptDeleteResult:
    script_id: str
    title: str
    raw_storage_path: Optional[str]
    storage_deleted: bool
    deleted_counts: dict[str, int] = field(default_factory=dict)


def delete_script_cascade(
    *,
    script_id: str,
    user_id: int,
    engine: Engine = default_engine,
) -> ScriptDeleteResult:
    """删除当前用户的一部剧本及其所有派生数据。

    数据库级联范围：
    scripts -> scenes -> evidence_refs
            -> reports -> evidence_refs
            -> script_feedback
            -> script_operations

    raw_storage_path 是文件系统资源，DB 事务提交后再删除。

    注：v1 起 `script_chunks` 表已删除（见 alembic 03_drop_script_chunks），不再统计。
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, title, raw_storage_path
                FROM scriptlens.scripts
                WHERE id = :sid AND user_id = :uid
                FOR UPDATE
                """
            ),
            {"sid": script_id, "uid": user_id},
        ).mappings().first()
        if not row:
            raise ScriptNotFoundError(script_id)

        counts = {
            "scenes": _count(conn, "scriptlens.scenes", script_id),
            "reports": _count(conn, "scriptlens.reports", script_id),
            "script_feedback": _count(conn, "scriptlens.script_feedback", script_id),
            "script_operations": _count(conn, "scriptlens.script_operations", script_id),
        }
        counts["evidence_refs"] = int(conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM scriptlens.evidence_refs er
                LEFT JOIN scriptlens.reports r ON r.id = er.report_id
                LEFT JOIN scriptlens.scenes s ON s.id = er.scene_id
                WHERE r.script_id = :sid OR s.script_id = :sid
                """
            ),
            {"sid": script_id},
        ).scalar_one())

        conn.execute(
            text(
                """
                DELETE FROM scriptlens.scripts
                WHERE id = :sid AND user_id = :uid
                """
            ),
            {"sid": script_id, "uid": user_id},
        )

    raw_storage_path = str(row["raw_storage_path"] or "")
    storage_deleted = _delete_storage_file(raw_storage_path)
    logger.info(
        "script.delete script_id=%s user_id=%s storage_deleted=%s counts=%s",
        script_id,
        user_id,
        storage_deleted,
        counts,
    )
    return ScriptDeleteResult(
        script_id=script_id,
        title=str(row["title"] or ""),
        raw_storage_path=raw_storage_path or None,
        storage_deleted=storage_deleted,
        deleted_counts=counts,
    )


def _count(conn, table_name: str, script_id: str) -> int:
    return int(conn.execute(
        text(f"SELECT COUNT(*) FROM {table_name} WHERE script_id = :sid"),
        {"sid": script_id},
    ).scalar_one())


def _delete_storage_file(raw_storage_path: str) -> bool:
    if not raw_storage_path:
        return False
    path = Path(raw_storage_path)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.exception("failed to delete script storage file path=%s", raw_storage_path)
        return False
