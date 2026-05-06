"""ScriptLens 操作历史服务（M4 timeline）。

写入/查询 `scriptlens.script_operations`，支持 doc-studio timeline 复用：
  - record_rewrite_op：rewrite 端点成功后落一条 op，snapshot_before/after 存原文与改写
  - list_operations：返回某剧本下所有 op，字段对齐 `DocStudioAPI.OperationSummary`
  - get_operation_snapshot：返回某 op 在某场景的 before/after 文本

故意不做：
  - 真正改 scenes.text（短剧场景里的"keep"对应的"应用改写"暂不持久化原文，
    避免毁掉用户的原始上传）。
  - upload 类 op（首次入库一次性把 50+ 场景塞进 jsonb 太重，timeline 第一节
    点用 created_at 字段就够了）。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


_VALID_INTENT_TYPES = {"rewrite", "upload", "manual_edit"}


class OperationError(Exception):
    """op 写入或查询失败（参数非法 / 剧本不存在 / 越权 / op 不存在）。"""


def record_rewrite_op(
    *,
    script_id: str,
    user_id: int,
    scene_id: str,
    target_dimension: str,
    issue: str,
    original_text: str,
    rewritten_text: str,
    rationale: str,
    success: bool = True,
    engine: Engine = default_engine,
) -> Dict[str, Any]:
    """rewrite 端点调用：把一次改写完整写入 timeline。

    snapshot_before / snapshot_after 用 `{scene_id: text}` 结构，对齐前端
    `fetchOperationSnapshotFile(version='before'|'after')` 的查询。
    """
    if not script_id or not scene_id:
        raise OperationError("script_id / scene_id 必填")

    op_id = str(uuid.uuid4())
    user_intent = f"AI 改写：{target_dimension} - {issue.strip()[:80]}"
    with engine.begin() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM scriptlens.scripts WHERE id = :sid"),
            {"sid": script_id},
        ).scalar()
        if owner is None:
            raise OperationError("剧本不存在")
        if int(owner) != int(user_id):
            raise OperationError("无权对该剧本写入 op 记录")

        conn.execute(
            text(
                """
                INSERT INTO scriptlens.script_operations
                    (id, script_id, user_id, intent_type, user_intent, success,
                     modified_files, snapshot_before, snapshot_after,
                     target_dimension, rationale, issue, created_at)
                VALUES
                    (:id, :sid, :uid, 'rewrite', :ui, :ok,
                     CAST(:mf AS JSONB), CAST(:sb AS JSONB), CAST(:sa AS JSONB),
                     :dim, :rat, :issue, NOW())
                """
            ),
            {
                "id": op_id,
                "sid": script_id,
                "uid": user_id,
                "ui": user_intent,
                "ok": bool(success),
                "mf": json.dumps([scene_id]),
                "sb": json.dumps({scene_id: original_text or ""}),
                "sa": json.dumps({scene_id: rewritten_text or ""}),
                "dim": target_dimension,
                "rat": rationale,
                "issue": issue,
            },
        )

    logger.info(
        "rewrite op recorded: op_id=%s script=%s user=%s scene=%s dim=%s",
        op_id, script_id, user_id, scene_id, target_dimension,
    )
    return {
        "operation_id": op_id,
        "script_id": script_id,
        "intent_type": "rewrite",
        "user_intent": user_intent,
        "success": success,
        "modified_files": [scene_id],
        "target_dimension": target_dimension,
        "rationale": rationale,
        "issue": issue,
    }


def list_operations(
    *,
    script_id: str,
    user_id: int,
    limit: int = 50,
    engine: Engine = default_engine,
) -> List[Dict[str, Any]]:
    """读某剧本下的全部 op，按 created_at 倒序。

    返回字段对齐 `DocStudioAPI.OperationSummary`：
        operation_id / workspace_id / user_id / timestamp / success
        intent_type / user_intent / modified_files / snapshot

    其中 `snapshot` 这里返回紧凑摘要（target_dimension / rationale / issue），
    避免把整段 before/after 文本塞进列表响应里——doc-studio UI 拿摘要够用，
    需要详细文本时走 `fetchOperationSnapshotFile`。
    """
    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM scriptlens.scripts WHERE id = :sid"),
            {"sid": script_id},
        ).scalar()
        if owner is None:
            raise OperationError("剧本不存在")
        if int(owner) != int(user_id):
            raise OperationError("无权查看该剧本的操作历史")

        result = conn.execute(
            text(
                """
                SELECT
                    id::text AS operation_id,
                    script_id::text AS workspace_id,
                    user_id,
                    created_at AS timestamp,
                    success,
                    intent_type,
                    user_intent,
                    modified_files,
                    target_dimension,
                    rationale,
                    issue
                FROM scriptlens.script_operations
                WHERE script_id = :sid
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            {"sid": script_id, "lim": int(limit)},
        ).mappings().all()

    rows: List[Dict[str, Any]] = []
    for r in result:
        d = dict(r)
        # modified_files 是 JSONB 数组，psycopg 返回 list[str]，原样透传
        mf = d.get("modified_files")
        if not isinstance(mf, list):
            mf = []
        d["modified_files"] = [str(p) for p in mf]
        # 把元数据塞进 snapshot 字段（前端 OperationSummary.snapshot 是 dict|null）
        d["snapshot"] = {
            "target_dimension": d.pop("target_dimension", None),
            "rationale": d.pop("rationale", None),
            "issue": d.pop("issue", None),
        }
        # timestamp 用 ISO8601 字符串（前端 Date.parse 兼容）
        ts = d.get("timestamp")
        if ts is not None and not isinstance(ts, str):
            d["timestamp"] = ts.isoformat()
        rows.append(d)
    return rows


def get_operation_snapshot(
    *,
    operation_id: str,
    user_id: int,
    file_path: str,
    version: str = "before",
    engine: Engine = default_engine,
) -> Dict[str, Any]:
    """取某 op 在某 scene 的 before/after 文本。

    返回结构对齐 `DocStudioAPI.FileContentResponse`：
        { path, content, encoding }

    `file_path` 在 ScriptLens 里就是 scene_id（前端 fileTree 把 scene_id 当 path）。
    `version='before'|'after'`，默认 before。
    """
    if version not in ("before", "after"):
        raise OperationError(f"非法 version={version}；只支持 before / after")
    if not operation_id or not file_path:
        raise OperationError("operation_id / file_path 必填")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    op.user_id AS owner_id,
                    op.snapshot_before,
                    op.snapshot_after
                FROM scriptlens.script_operations op
                WHERE op.id = :op
                """
            ),
            {"op": operation_id},
        ).mappings().first()

    if row is None:
        raise OperationError("操作记录不存在")
    if int(row["owner_id"]) != int(user_id):
        raise OperationError("无权查看该操作的快照")

    snapshot = row["snapshot_before"] if version == "before" else row["snapshot_after"]
    if not isinstance(snapshot, dict):
        snapshot = {}
    content = snapshot.get(file_path)
    if content is None:
        # 这个 op 没有改这个 scene，返回空内容（前端会 fallback 到 fallbackOriginal）
        content = ""

    return {
        "path": file_path,
        "content": str(content),
        "encoding": "utf-8",
    }
