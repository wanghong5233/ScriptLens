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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


_VALID_INTENT_TYPES = {"rewrite", "upload", "manual_edit"}


class OperationError(Exception):
    """op 写入或查询失败（参数非法 / 剧本不存在 / 越权 / op 不存在）。"""


OperationSource = Literal["db", "history"]


@dataclass(frozen=True)
class OperationLocator:
    source: OperationSource
    raw_id: str


def _build_operation_ref(source: OperationSource, raw_id: str) -> str:
    return f"{source}:{raw_id}"


def _parse_operation_uuid(raw_id: str) -> Optional[uuid.UUID]:
    value = str(raw_id or "").strip()
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _parse_operation_locator(operation_id: str) -> OperationLocator:
    raw = str(operation_id or "").strip()
    if not raw:
        raise OperationError("operation_id 不能为空")

    source, sep, payload = raw.partition(":")
    source = source.strip().lower()
    payload = payload.strip()
    if not sep or source not in {"db", "history"} or not payload:
        raise OperationError("operation_id 格式非法：必须是 db:<uuid> 或 history:<id>")
    if source == "db":
        parsed = _parse_operation_uuid(payload)
        if parsed is None:
            raise OperationError("db operation_id 非法：必须是 UUID")
        return OperationLocator(source="db", raw_id=str(parsed))
    return OperationLocator(source="history", raw_id=payload)


def _resolve_agent_history_root(script_id: str, user_id: int) -> Path:
    from agent_runtime.core.config import settings as agent_settings

    return (
        Path(agent_settings.WORKSPACES_ROOT)
        / str(int(user_id))
        / str(script_id)
        / ".agent_history"
    )


def _load_agent_history_operation_payload(
    *,
    script_id: str,
    operation_id: str,
    user_id: int,
) -> tuple[Path, Dict[str, Any]]:
    history_root = _resolve_agent_history_root(script_id, user_id)
    op_payload_path = history_root / "operations" / f"{operation_id}.json"
    if not op_payload_path.exists():
        raise OperationError("操作记录不存在")

    try:
        op_payload = json.loads(op_payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError(f"读取操作记录失败: {exc}") from exc
    if not isinstance(op_payload, dict):
        raise OperationError("操作记录格式非法")

    payload_user = op_payload.get("user_id")
    payload_workspace = op_payload.get("workspace_id")
    if str(payload_workspace or "") != str(script_id):
        raise OperationError("操作记录不属于当前剧本")
    try:
        if int(payload_user) != int(user_id):
            raise OperationError("无权查看该操作的快照")
    except (TypeError, ValueError) as exc:
        raise OperationError(f"操作记录 user_id 非法: {exc}") from exc
    return history_root, op_payload


def _resolve_agent_snapshot_manifest_path(
    *,
    history_root: Path,
    op_payload: Dict[str, Any],
) -> Path:
    snapshot_meta = op_payload.get("snapshot")
    if not isinstance(snapshot_meta, dict):
        raise OperationError("该操作未持久化快照")
    snapshot_rel = str(snapshot_meta.get("path") or "").strip()
    if not snapshot_rel:
        raise OperationError("该操作未持久化快照")

    snapshot_path = history_root / snapshot_rel
    try:
        resolved_snapshot_path = snapshot_path.resolve()
    except OSError as exc:
        raise OperationError(f"快照路径非法: {exc}") from exc
    if not str(resolved_snapshot_path).startswith(str(history_root.resolve())):
        raise OperationError("快照路径越界")
    if not resolved_snapshot_path.exists():
        raise OperationError("快照文件不存在")
    return resolved_snapshot_path


def _read_snapshot_blob(history_root: Path, digest: str) -> str:
    safe_digest = str(digest or "").strip().lower()
    if len(safe_digest) != 64 or any(ch not in "0123456789abcdef" for ch in safe_digest):
        raise OperationError("快照 blob 校验失败")
    blob_path = history_root / "blobs" / safe_digest[:2] / f"{safe_digest}.txt"
    if not blob_path.exists():
        raise OperationError("快照 blob 不存在")
    try:
        return blob_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationError(f"读取快照 blob 失败: {exc}") from exc


def _load_agent_history_snapshot(
    *,
    script_id: str,
    operation_id: str,
    user_id: int,
    file_path: str,
    version: str,
) -> Dict[str, Any]:
    history_root, op_payload = _load_agent_history_operation_payload(
        script_id=script_id,
        operation_id=operation_id,
        user_id=user_id,
    )
    resolved_snapshot_path = _resolve_agent_snapshot_manifest_path(
        history_root=history_root,
        op_payload=op_payload,
    )

    try:
        manifest = json.loads(resolved_snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError(f"读取快照文件失败: {exc}") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        raise OperationError("快照文件格式非法")

    matched_entry: Optional[Dict[str, Any]] = None
    for item in files:
        if not isinstance(item, dict):
            continue
        if str(item.get("path") or "").strip() == file_path:
            matched_entry = item
            break
    if matched_entry is None:
        raise OperationError("该操作不包含目标文件快照")

    blob_key = "before_blob" if version == "before" else "after_blob"
    blob_digest = str(matched_entry.get(blob_key) or "").strip()
    if blob_digest:
        content = _read_snapshot_blob(history_root, blob_digest)
    elif version == "before":
        # before 没有 blob 时，通常表示该文件是新建；返回空串给前端兜底逻辑。
        content = ""
    else:
        # after blob 默认关闭持久化（AGENT_HISTORY_PERSIST_AFTER_SNAPSHOT=false）。
        # 这里做 best-effort：按当前 ScriptVFS 内容返回，避免直接报错。
        try:
            from agent_runtime.service.script_vfs import ScriptVFS, ScriptVFSError

            vfs = ScriptVFS(script_id=script_id)
            content = vfs.read(file_path)
        except (ScriptVFSError, ValueError):
            content = ""

    return {
        "path": file_path,
        "content": str(content),
        "encoding": "utf-8",
    }


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
        scene_owner = conn.execute(
            text("SELECT script_id::text FROM scriptlens.scenes WHERE id = :scene_id"),
            {"scene_id": scene_id},
        ).scalar()
        if scene_owner is None:
            raise OperationError("场景不存在")
        if str(scene_owner) != str(script_id):
            raise OperationError("场景不属于当前剧本，拒绝写入 timeline")

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
        "operation_id": _build_operation_ref("db", op_id),
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

    operation_id 对外统一为显式来源协议：`db:<uuid>`。

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
        raw_operation_id = str(d.get("operation_id") or "").strip()
        d["operation_id"] = (
            _build_operation_ref("db", raw_operation_id) if raw_operation_id else ""
        )
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


def get_rewrite_task_status_map(
    *,
    script_id: str,
    user_id: int,
    engine: Engine = default_engine,
) -> Dict[str, Dict[str, Any]]:
    """从 script_operations 派生 (scene_id, dimension) 上的改写任务状态。

    返回 dict[`{scene_id}:{dimension}`] -> {
        attempts, last_op_id, last_status, last_at
    }

    用于 ScriptViewResponse.task_status，前端在报告卡片右上角渲染状态徽章
    （详见 docs/03-system-mental-model.md §8）。

    last_status 当前只有 `accepted | rejected` 两态：
        - 现阶段 record_rewrite_op 仅在 router /rewrite 成功落库后调用一次（success=True）；
        - 失败 case 走 success=False；
        - 未来 chat 里的 propose_rewrite_tool 落 op 后会扩出 `proposed` 态（diff 已产但用户没接受）。
    """
    if not script_id:
        return {}
    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM scriptlens.scripts WHERE id = :sid"),
            {"sid": script_id},
        ).scalar()
        if owner is None:
            raise OperationError("剧本不存在")
        if int(owner) != int(user_id):
            raise OperationError("无权查看该剧本的任务状态")

        rows = conn.execute(
            text(
                """
                SELECT
                    modified_files->>0 AS scene_id,
                    target_dimension AS dimension,
                    COUNT(*)::int AS attempts,
                    MAX(created_at) AS last_at,
                    (array_agg(id::text ORDER BY created_at DESC))[1] AS last_op_id,
                    (array_agg(success ORDER BY created_at DESC))[1] AS last_success
                FROM scriptlens.script_operations
                WHERE script_id = :sid
                  AND intent_type = 'rewrite'
                  AND target_dimension IS NOT NULL
                  AND jsonb_typeof(modified_files) = 'array'
                  AND jsonb_array_length(modified_files) > 0
                GROUP BY modified_files->>0, target_dimension
                """
            ),
            {"sid": script_id},
        ).mappings().all()

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        scene_id = r.get("scene_id")
        dimension = r.get("dimension")
        if not scene_id or not dimension:
            continue
        last_at = r.get("last_at")
        out[f"{scene_id}:{dimension}"] = {
            "attempts": int(r.get("attempts") or 0),
            "last_op_id": (
                _build_operation_ref("db", str(r.get("last_op_id")))
                if r.get("last_op_id")
                else None
            ),
            "last_status": "accepted" if r.get("last_success") else "rejected",
            "last_at": last_at.isoformat() if last_at is not None and not isinstance(last_at, str) else last_at,
        }
    return out


def validate_operation_access(
    *,
    script_id: str,
    operation_id: str,
    user_id: int,
    engine: Engine = default_engine,
) -> OperationLocator:
    """校验 operation 归属并返回解析后的 locator。"""
    locator = _parse_operation_locator(operation_id)
    if locator.source == "history":
        _load_agent_history_operation_payload(
            script_id=script_id,
            operation_id=locator.raw_id,
            user_id=user_id,
        )
        return locator

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        op.user_id AS owner_id,
                        op.script_id::text AS script_id
                    FROM scriptlens.script_operations op
                    WHERE op.id = :op
                    """
                ),
                {"op": locator.raw_id},
            ).mappings().first()
    except SQLAlchemyError as exc:
        raise OperationError(f"查询操作记录失败: {exc}") from exc

    if row is None:
        raise OperationError("操作记录不存在")
    if int(row["owner_id"]) != int(user_id):
        raise OperationError("无权查看该操作的快照")
    if str(row["script_id"]) != str(script_id):
        raise OperationError("操作记录不属于当前剧本")
    return locator


def get_operation_snapshot(
    *,
    script_id: str,
    operation_id: str,
    user_id: int,
    file_path: str,
    version: str = "before",
    engine: Engine = default_engine,
) -> Dict[str, Any]:
    """取某 op 在某 scene 的 before/after 文本。

    返回结构对齐 `DocStudioAPI.FileContentResponse`：
        { path, content, encoding }

    `file_path` 在 ScriptLens 里使用 ScriptVFS 路径（如 scenes/E03-S005.txt）。
    兼容历史 op：若 snapshot 仍以 scene_id 作为 key，会自动映射。
    `version='before'|'after'`，默认 before。
    """
    if version not in ("before", "after"):
        raise OperationError(f"非法 version={version}；只支持 before / after")
    if not script_id or not operation_id or not file_path:
        raise OperationError("script_id / operation_id / file_path 必填")

    locator = validate_operation_access(
        script_id=script_id,
        operation_id=operation_id,
        user_id=user_id,
        engine=engine,
    )
    if locator.source == "history":
        return _load_agent_history_snapshot(
            script_id=script_id,
            operation_id=locator.raw_id,
            user_id=user_id,
            file_path=file_path,
            version=version,
        )

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        op.snapshot_before,
                        op.snapshot_after
                    FROM scriptlens.script_operations op
                    WHERE op.id = :op
                    """
                ),
                {"op": locator.raw_id},
            ).mappings().first()
    except SQLAlchemyError as exc:
        raise OperationError(f"查询操作快照失败: {exc}") from exc

    if row is None:
        raise OperationError("操作记录不存在")

    snapshot = row["snapshot_before"] if version == "before" else row["snapshot_after"]
    if not isinstance(snapshot, dict):
        snapshot = {}
    content = snapshot.get(file_path)

    if content is None:
        # 兼容 ScriptVFS 路径：历史 op 里可能存 scene_id，前端请求传 scenes/E03-S005.txt。
        try:
            from agent_runtime.service.script_vfs import ScriptVFS, ScriptVFSError

            vfs = ScriptVFS(script_id=script_id)
            if file_path.startswith("scenes/"):
                scene_id = vfs.resolve_scene_id(file_path)
                content = snapshot.get(scene_id)
            else:
                normalized_path = vfs.coerce_file_path(file_path)
                content = snapshot.get(normalized_path)
                if content is None:
                    scene_id = vfs.resolve_scene_id(normalized_path)
                    content = snapshot.get(scene_id)
        except (ScriptVFSError, ValueError):
            content = None

    if content is None:
        # 这个 op 没有改这个 scene，返回空内容（前端会 fallback 到 fallbackOriginal）
        content = ""

    return {
        "path": file_path,
        "content": str(content),
        "encoding": "utf-8",
    }
