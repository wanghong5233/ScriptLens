"""
工作区文件与目录操作工具。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from .base_tool import BaseTool, ToolResult
from .workspace_utils import ensure_parent_directory, get_workspace_path, resolve_path_within_workspace

logger = logging.getLogger(__name__)


def _relative_path(target: Path, workspace_path: Path) -> str:
    """Convert absolute path to workspace-relative POSIX style path."""
    try:
        rel = target.relative_to(workspace_path)
        text = str(rel).replace("\\", "/")
        return text or "."
    except ValueError:
        return str(target).replace("\\", "/")


def _validate_content_with_extension(file_path: str, content: str) -> List[str]:
    """
    基于扩展名做轻量一致性校验，返回警告列表（不阻断写入）。
    """
    warnings: List[str] = []
    suffix = Path(file_path).suffix.lower()
    text = str(content or "")
    stripped = text.lstrip()
    lowered = stripped.lower()

    if suffix in {".md", ".markdown"} and stripped:
        markdown_markers = ("# ", "## ", "- ", "* ", "1. ", "> ", "```")
        has_markdown_hint = any(marker in stripped for marker in markdown_markers)
        if not has_markdown_hint and len(stripped) > 120:
            warnings.append("文件扩展名为 Markdown，但内容缺少常见 Markdown 结构标记。")

    if suffix == ".json" and stripped:
        try:
            json.loads(stripped)
        except Exception:
            warnings.append("文件扩展名为 JSON，但内容不是合法 JSON。")

    if suffix == ".html" and stripped and "<html" not in lowered and "<!doctype html" not in lowered:
        warnings.append("文件扩展名为 HTML，但内容未检测到常见 HTML 根结构。")

    return warnings


class ListWorkspaceTreeTool(BaseTool):
    """
    列出工作区目录结构，帮助 Agent 选择生成位置。
    """

    def __init__(self):
        super().__init__(
            name="list_workspace_tree_tool",
            description=(
                "列出工作区目录/文件结构（类似 ls/tree）。"
                "用于在创建文件前先确认目标目录。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "可选，起始目录（相对工作区），默认 '.'",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "最大展开深度（默认 3，范围 0-8）",
                    "default": 3,
                },
                "max_entries": {
                    "type": "integer",
                    "description": "最大返回条目数（默认 200，范围 20-1200）",
                    "default": 200,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "是否包含隐藏文件/目录（默认 false）",
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any],
    ) -> ToolResult:
        target_path = str(parameters.get("target_path") or ".").strip() or "."
        max_depth = max(0, min(int(parameters.get("max_depth", 3) or 3), 8))
        max_entries = max(20, min(int(parameters.get("max_entries", 200) or 200), 1200))
        include_hidden = bool(parameters.get("include_hidden", False))

        try:
            workspace_path = get_workspace_path(agent_state)
            target = resolve_path_within_workspace(workspace_path, target_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if not target.exists():
            return ToolResult(success=False, error=f"路径不存在: {target_path}")

        try:
            payload = await asyncio.to_thread(
                self._collect_entries,
                workspace_path,
                target,
                max_depth,
                max_entries,
                include_hidden,
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"列出目录失败: {exc}")

        summary_suffix = "（已截断）" if payload["truncated"] else ""
        return ToolResult(
            success=True,
            data=payload,
            summary=f"已列出 {payload['target_path']} 下 {len(payload['entries'])} 项{summary_suffix}",
        )

    def _collect_entries(
        self,
        workspace_path: Path,
        target: Path,
        max_depth: int,
        max_entries: int,
        include_hidden: bool,
    ) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = []
        truncated = False
        target_rel = _relative_path(target, workspace_path)

        if target.is_file():
            entries.append(
                {
                    "path": target_rel,
                    "type": "file",
                    "depth": 0,
                    "size_bytes": self._safe_size(target),
                }
            )
            return {
                "workspace_root": str(workspace_path),
                "target_path": target_rel,
                "entries": entries,
                "truncated": False,
                "max_depth": max_depth,
                "max_entries": max_entries,
            }

        queue: List[tuple[Path, int]] = [(target, 0)]
        visited_dirs: set[str] = set()

        while queue and len(entries) < max_entries:
            current_dir, depth = queue.pop(0)
            current_key = str(current_dir)
            if current_key in visited_dirs:
                continue
            visited_dirs.add(current_key)

            if depth > max_depth:
                continue

            if depth > 0:
                entries.append(
                    {
                        "path": _relative_path(current_dir, workspace_path),
                        "type": "directory",
                        "depth": depth,
                    }
                )
                if len(entries) >= max_entries:
                    truncated = True
                    break

            if depth == max_depth:
                continue

            try:
                children = sorted(
                    list(current_dir.iterdir()),
                    key=lambda p: (p.is_file(), p.name.lower()),
                )
            except OSError:
                continue

            for child in children:
                if not include_hidden and child.name.startswith("."):
                    continue
                if child.name == "__pycache__":
                    continue
                if child.name == ".agent_history":
                    continue
                rel_path = _relative_path(child, workspace_path)
                child_depth = depth + 1
                if child.is_dir():
                    queue.append((child, child_depth))
                    continue
                entries.append(
                    {
                        "path": rel_path,
                        "type": "file",
                        "depth": child_depth,
                        "size_bytes": self._safe_size(child),
                    }
                )
                if len(entries) >= max_entries:
                    truncated = True
                    break
            if truncated:
                break

        return {
            "workspace_root": str(workspace_path),
            "target_path": target_rel,
            "entries": entries,
            "truncated": truncated,
            "max_depth": max_depth,
            "max_entries": max_entries,
        }

    @staticmethod
    def _safe_size(target: Path) -> int:
        try:
            return int(target.stat().st_size)
        except OSError:
            return 0


class CreateDirectoryTool(BaseTool):
    """
    创建目录工具。
    """

    def __init__(self):
        super().__init__(
            name="create_directory_tool",
            description="在工作区中创建目录（支持递归创建父目录）。",
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "directory_path": {
                    "type": "string",
                    "description": "要创建的目录路径（相对工作区）",
                },
                "exist_ok": {
                    "type": "boolean",
                    "description": "目录已存在时是否视为成功（默认 true）",
                    "default": True,
                },
            },
            "required": ["directory_path"],
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any],
    ) -> ToolResult:
        directory_path = str(
            parameters.get("directory_path")
            or parameters.get("path")
            or ""
        ).strip()
        if not directory_path:
            return ToolResult(success=False, error="directory_path 参数不能为空")
        exist_ok = bool(parameters.get("exist_ok", True))

        try:
            workspace_path = get_workspace_path(agent_state)
            target = resolve_path_within_workspace(workspace_path, directory_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if target.exists():
            if target.is_dir():
                if exist_ok:
                    return ToolResult(
                        success=True,
                        data={
                            "directory_path": _relative_path(target, workspace_path),
                            "created": False,
                            "already_exists": True,
                        },
                        summary=f"目录已存在：{_relative_path(target, workspace_path)}",
                    )
                return ToolResult(success=False, error=f"目录已存在: {directory_path}")
            return ToolResult(success=False, error=f"目标路径已存在且不是目录: {directory_path}")

        try:
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=False)
        except Exception as exc:
            return ToolResult(success=False, error=f"创建目录失败: {exc}")

        rel_path = _relative_path(target, workspace_path)
        return ToolResult(
            success=True,
            data={
                "directory_path": rel_path,
                "created": True,
                "already_exists": False,
            },
            summary=f"已创建目录：{rel_path}",
        )


class CreateFileTool(BaseTool):
    """
    创建或覆盖文本文件工具。
    """

    def __init__(self):
        super().__init__(
            name="create_file_tool",
            description=(
                "在工作区创建文本文件（可选覆盖已有文件）。"
                "适用于生成新的 Markdown/代码文件。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "目标文件路径（相对工作区）",
                },
                "content": {
                    "type": "string",
                    "description": "文件内容，默认空字符串",
                    "default": "",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "文件已存在时是否覆盖（默认 false）",
                    "default": False,
                },
                "create_parent_dirs": {
                    "type": "boolean",
                    "description": "父目录不存在时是否自动创建（默认 true）",
                    "default": True,
                },
                "encoding": {
                    "type": "string",
                    "description": "文本编码（默认 utf-8）",
                    "default": "utf-8",
                },
            },
            "required": ["file_path"],
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any],
    ) -> ToolResult:
        file_path = str(
            parameters.get("file_path")
            or parameters.get("path")
            or ""
        ).strip()
        if not file_path:
            return ToolResult(success=False, error="file_path 参数不能为空")

        content = str(parameters.get("content") or "")
        overwrite = bool(parameters.get("overwrite", False))
        create_parent_dirs = bool(parameters.get("create_parent_dirs", True))
        encoding = str(parameters.get("encoding") or "utf-8").strip() or "utf-8"

        try:
            workspace_path = get_workspace_path(agent_state)
            target = resolve_path_within_workspace(workspace_path, file_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if target.exists() and target.is_dir():
            return ToolResult(success=False, error=f"目标路径是目录，不能写入文件: {file_path}")

        existed = target.exists()
        if existed and not overwrite:
            return ToolResult(success=False, error=f"文件已存在，请设置 overwrite=true: {file_path}")

        if create_parent_dirs:
            ensure_parent_directory(target)
        elif not target.parent.exists():
            return ToolResult(success=False, error=f"父目录不存在: {_relative_path(target.parent, workspace_path)}")

        try:
            await asyncio.to_thread(target.write_text, content, encoding)
        except LookupError:
            return ToolResult(success=False, error=f"无效编码: {encoding}")
        except Exception as exc:
            return ToolResult(success=False, error=f"写入文件失败: {exc}")

        rel_path = _relative_path(target, workspace_path)
        if hasattr(agent_state, "modified_files") and isinstance(agent_state.modified_files, set):
            agent_state.modified_files.add(rel_path)

        created = not existed
        size_bytes = 0
        try:
            size_bytes = int(target.stat().st_size)
        except OSError:
            size_bytes = len(content.encode("utf-8", errors="ignore"))
        validation_warnings = _validate_content_with_extension(rel_path, content)
        if validation_warnings:
            logger.info(
                "CreateFileTool validation warnings for %s: %s",
                rel_path,
                " | ".join(validation_warnings),
            )

        action = "创建" if created else "覆盖"
        return ToolResult(
            success=True,
            data={
                "file_path": rel_path,
                "created": created,
                "overwritten": existed and overwrite,
                "size_bytes": size_bytes,
                "encoding": encoding,
                "validation_passed": len(validation_warnings) == 0,
                "validation_warnings": validation_warnings,
            },
            summary=(
                f"已{action}文件：{rel_path}（{size_bytes} 字节）"
                + ("，含扩展名一致性校验提示" if validation_warnings else "")
            ),
        )


class RenameMovePathTool(BaseTool):
    """
    重命名/移动文件或目录工具。
    """

    def __init__(self):
        super().__init__(
            name="rename_move_path_tool",
            description="重命名或移动工作区内的文件/目录（支持可控覆盖）。",
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "原路径（相对工作区）",
                },
                "target_path": {
                    "type": "string",
                    "description": "目标路径（相对工作区）",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "目标已存在时是否覆盖（默认 false）",
                    "default": False,
                },
                "create_parent_dirs": {
                    "type": "boolean",
                    "description": "目标父目录不存在时是否自动创建（默认 true）",
                    "default": True,
                },
            },
            "required": ["source_path", "target_path"],
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any],
    ) -> ToolResult:
        source_path = str(parameters.get("source_path") or "").strip()
        target_path = str(parameters.get("target_path") or "").strip()
        overwrite = bool(parameters.get("overwrite", False))
        create_parent_dirs = bool(parameters.get("create_parent_dirs", True))

        if not source_path or not target_path:
            return ToolResult(success=False, error="source_path/target_path 参数不能为空")
        if source_path == target_path:
            return ToolResult(success=False, error="target_path 必须与 source_path 不同")

        try:
            workspace_path = get_workspace_path(agent_state)
            source = resolve_path_within_workspace(workspace_path, source_path)
            target = resolve_path_within_workspace(workspace_path, target_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if source == workspace_path or target == workspace_path:
            return ToolResult(success=False, error="不允许直接操作工作区根目录")
        if not source.exists():
            return ToolResult(success=False, error=f"源路径不存在: {source_path}")

        source_is_dir = source.is_dir()
        target_exists = target.exists()
        if source_is_dir:
            try:
                target.relative_to(source)
                return ToolResult(success=False, error="不能将目录移动到其自身子目录中")
            except ValueError:
                pass

        if target_exists and not overwrite:
            return ToolResult(success=False, error=f"目标路径已存在，请设置 overwrite=true: {target_path}")

        if target_exists and overwrite:
            if source_is_dir and target.is_file():
                return ToolResult(success=False, error="目录不能覆盖文件类型目标")
            if (not source_is_dir) and target.is_dir():
                return ToolResult(success=False, error="文件不能覆盖目录类型目标")
            try:
                if target.is_dir():
                    await asyncio.to_thread(shutil.rmtree, target)
                else:
                    await asyncio.to_thread(target.unlink)
            except Exception as exc:
                return ToolResult(success=False, error=f"覆盖目标失败: {exc}")

        if create_parent_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            return ToolResult(success=False, error=f"目标父目录不存在: {_relative_path(target.parent, workspace_path)}")

        try:
            await asyncio.to_thread(shutil.move, str(source), str(target))
        except Exception as exc:
            return ToolResult(success=False, error=f"移动/重命名失败: {exc}")

        source_rel = _relative_path(source, workspace_path)
        target_rel = _relative_path(target, workspace_path)
        if hasattr(agent_state, "modified_files") and isinstance(agent_state.modified_files, set):
            if not source_is_dir:
                agent_state.modified_files.add(source_rel)
                agent_state.modified_files.add(target_rel)

        logger.info(
            "RenameMovePathTool moved %s -> %s (overwrite=%s)",
            source_rel,
            target_rel,
            overwrite,
        )
        return ToolResult(
            success=True,
            data={
                "source_path": source_rel,
                "target_path": target_rel,
                "type": "directory" if source_is_dir else "file",
                "overwritten": bool(target_exists and overwrite),
            },
            summary=f"已移动/重命名：{source_rel} -> {target_rel}",
        )


class DeletePathTool(BaseTool):
    """
    删除文件或目录工具（带安全防护）。
    """

    _CONFIRM_TTL_SECONDS = 300
    _PREVIEW_SAMPLE_LIMIT = 40
    _PREVIEW_SCAN_LIMIT = 20000

    def __init__(self):
        super().__init__(
            name="delete_path_tool",
            description=(
                "删除工作区内文件或目录。删除非空目录需显式设置 recursive=true，"
                "并通过用户交互确认后才会真正执行，避免误删。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "目标路径（相对工作区）",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "删除目录时是否递归删除（默认 false）",
                    "default": False,
                },
                "missing_ok": {
                    "type": "boolean",
                    "description": "路径不存在时是否视为成功（默认 false）",
                    "default": False,
                },
            },
            "required": ["target_path"],
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any],
    ) -> ToolResult:
        target_path = str(parameters.get("target_path") or parameters.get("path") or "").strip()
        recursive = bool(parameters.get("recursive", False))
        missing_ok = bool(parameters.get("missing_ok", False))
        approval_token = str(
            parameters.get("_approval_token") or parameters.get("approval_token") or ""
        ).strip()
        is_approved_execution = bool(approval_token)
        if not target_path:
            return ToolResult(success=False, error="target_path 参数不能为空")

        try:
            workspace_path = get_workspace_path(agent_state)
            target = resolve_path_within_workspace(workspace_path, target_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if target == workspace_path:
            return ToolResult(success=False, error="不允许删除工作区根目录")
        if not target.exists():
            if missing_ok:
                return ToolResult(
                    success=True,
                    data={
                        "target_path": target_path,
                        "deleted": False,
                        "already_missing": True,
                    },
                    summary=f"路径不存在，已按 missing_ok 跳过：{target_path}",
                )
            return ToolResult(success=False, error=f"目标路径不存在: {target_path}")

        deleted_type = "directory" if target.is_dir() else "file"
        target_rel = _relative_path(target, workspace_path)
        has_children = False
        if target.is_dir():
            try:
                has_children = any(True for _ in target.iterdir())
            except Exception as exc:
                return ToolResult(success=False, error=f"读取目录失败: {exc}")

        can_execute = not (deleted_type == "directory" and has_children and not recursive)
        if not is_approved_execution:
            preview = self._build_preview(
                target=target,
                workspace_path=workspace_path,
                deleted_type=deleted_type,
            )
            if not can_execute:
                return ToolResult(
                    success=False,
                    error="目录非空，若确认删除请设置 recursive=true 后重试。",
                    data={
                        "target_path": target_rel,
                        "type": deleted_type,
                        "recursive": recursive,
                        "can_execute": False,
                        "preview": preview,
                    },
                    summary=f"删除请求参数不足：{target_rel}",
                )
            token, expires_in_seconds = self._issue_approval_token(
                agent_state=agent_state,
                target_path=target_rel,
                recursive=recursive,
                deleted_type=deleted_type,
            )
            return ToolResult(
                success=True,
                data={
                    "target_path": target_rel,
                    "type": deleted_type,
                    "recursive": recursive,
                    "can_execute": can_execute,
                    "interaction_required": True,
                    "interaction_type": "dangerous_action_confirm",
                    "title": "确认删除路径",
                    "message": (
                        f"即将删除 {target_rel}。请确认是否继续。"
                        if can_execute
                        else f"{target_rel} 当前不可删除，请先调整参数后重试。"
                    ),
                    "approval_token": token,
                    "timeout_seconds": (
                        expires_in_seconds if expires_in_seconds > 0 else self._CONFIRM_TTL_SECONDS
                    ),
                    "preview": preview,
                },
                summary=f"删除请求待确认：{target_rel}",
            )

        # 高安全模式：真实删除必须携带系统签发的审批令牌。
        if not approval_token:
            return ToolResult(
                success=False,
                error="缺少 approval_token。请先触发用户确认交互。",
            )
        token_error = self._validate_approval_token(
            agent_state=agent_state,
            approval_token=approval_token,
            target_path=target_rel,
            recursive=recursive,
            deleted_type=deleted_type,
        )
        if token_error:
            return ToolResult(success=False, error=token_error)
        if not can_execute:
            return ToolResult(
                success=False,
                error="目录非空，若确认删除请设置 recursive=true 后重新触发用户确认。",
            )

        try:
            if target.is_dir():
                if has_children:
                    await asyncio.to_thread(shutil.rmtree, target)
                else:
                    await asyncio.to_thread(target.rmdir)
            else:
                await asyncio.to_thread(target.unlink)
        except Exception as exc:
            return ToolResult(success=False, error=f"删除失败: {exc}")
        finally:
            self._consume_approval_token(agent_state, approval_token)

        if (
            hasattr(agent_state, "modified_files")
            and isinstance(agent_state.modified_files, set)
            and deleted_type == "file"
        ):
            agent_state.modified_files.add(target_rel)

        logger.info(
            "DeletePathTool deleted %s (type=%s, recursive=%s)",
            target_rel,
            deleted_type,
            recursive,
        )
        return ToolResult(
            success=True,
            data={
                "target_path": target_rel,
                "deleted": True,
                "type": deleted_type,
                "recursive": recursive,
            },
            summary=f"已删除{deleted_type}：{target_rel}",
        )

    def _get_approval_store(self, agent_state: Any) -> Dict[str, Dict[str, Any]]:
        store = getattr(agent_state, "pending_delete_confirmations", None)
        if not isinstance(store, dict):
            store = {}
            setattr(agent_state, "pending_delete_confirmations", store)
        return store

    def _prune_expired_approval_tokens(self, store: Dict[str, Dict[str, Any]]) -> None:
        now = time.time()
        expired_tokens = [
            token
            for token, payload in store.items()
            if float(payload.get("expires_at", 0.0)) <= now
        ]
        for token in expired_tokens:
            store.pop(token, None)

    def _issue_approval_token(
        self,
        *,
        agent_state: Any,
        target_path: str,
        recursive: bool,
        deleted_type: str,
    ) -> tuple[str, int]:
        store = self._get_approval_store(agent_state)
        self._prune_expired_approval_tokens(store)
        now = time.time()
        expires_at = now + self._CONFIRM_TTL_SECONDS
        token = uuid.uuid4().hex[:18]
        store[token] = {
            "target_path": target_path,
            "recursive": bool(recursive),
            "deleted_type": deleted_type,
            "issued_at": now,
            "expires_at": expires_at,
        }
        return token, self._CONFIRM_TTL_SECONDS

    def _validate_approval_token(
        self,
        *,
        agent_state: Any,
        approval_token: str,
        target_path: str,
        recursive: bool,
        deleted_type: str,
    ) -> str:
        store = self._get_approval_store(agent_state)
        self._prune_expired_approval_tokens(store)
        payload = store.get(approval_token)
        if not isinstance(payload, dict):
            return "approval_token 无效或已过期，请重新触发用户确认。"
        if str(payload.get("target_path") or "") != target_path:
            return "approval_token 与当前 target_path 不匹配，请重新触发用户确认。"
        if bool(payload.get("recursive")) != bool(recursive):
            return "approval_token 与当前 recursive 参数不匹配，请重新触发用户确认。"
        if str(payload.get("deleted_type") or "") != deleted_type:
            return "approval_token 与当前目标类型不匹配，请重新触发用户确认。"
        return ""

    def _consume_approval_token(self, agent_state: Any, approval_token: str) -> None:
        if not approval_token:
            return
        store = self._get_approval_store(agent_state)
        store.pop(approval_token, None)
        self._prune_expired_approval_tokens(store)

    def _build_preview(
        self,
        *,
        target: Path,
        workspace_path: Path,
        deleted_type: str,
    ) -> Dict[str, Any]:
        if deleted_type == "file":
            size_bytes = 0
            try:
                size_bytes = int(target.stat().st_size)
            except OSError:
                size_bytes = 0
            return {
                "exists": True,
                "file_size_bytes": size_bytes,
                "sample_paths": [_relative_path(target, workspace_path)],
            }

        file_count = 0
        dir_count = 0
        scanned = 0
        truncated = False
        sample_paths: List[str] = []
        for root, dirs, files in os.walk(target):
            dir_count += len(dirs)
            file_count += len(files)
            for name in [*dirs, *files]:
                scanned += 1
                if len(sample_paths) < self._PREVIEW_SAMPLE_LIMIT:
                    sample_paths.append(
                        _relative_path(Path(root) / name, workspace_path)
                    )
                if scanned >= self._PREVIEW_SCAN_LIMIT:
                    truncated = True
                    break
            if truncated:
                break

        return {
            "exists": True,
            "directory_count": dir_count,
            "file_count": file_count,
            "sample_paths": sample_paths,
            "truncated": truncated,
        }
