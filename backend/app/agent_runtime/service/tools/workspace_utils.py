"""
工作区辅助工具
"""
from pathlib import Path
from typing import Any, Optional
import os

from ...core.config import settings


def get_workspace_path(agent_state: Any, workspace_id: Optional[str] = None) -> Path:
    """根据 AgentState 获取工作区路径"""
    resolved_workspace_id = workspace_id or getattr(agent_state, "workspace_id", None)
    user_id = getattr(agent_state, "user_id", None)

    if resolved_workspace_id is None or user_id is None:
        raise ValueError("Agent state 缺少 workspace_id 或 user_id")

    workspace_path = (
        Path(settings.WORKSPACES_ROOT)
        / str(user_id)
        / str(resolved_workspace_id)
    )
    return Path(os.path.abspath(str(workspace_path)))


def resolve_path_within_workspace(workspace_path: Path, target_path: str) -> Path:
    """解析工作区内的文件相对路径，防止目录逃逸"""
    if not target_path:
        raise ValueError("目标路径不能为空")

    if os.path.isabs(target_path):
        resolved_target = Path(os.path.abspath(target_path))
    else:
        resolved_target = Path(os.path.abspath(os.path.join(workspace_path, target_path)))

    if not str(resolved_target).startswith(str(workspace_path)):
        raise ValueError("目标路径不在工作区内")

    return resolved_target


def ensure_parent_directory(path: Path):
    """确保文件父目录存在"""
    path.parent.mkdir(parents=True, exist_ok=True)

