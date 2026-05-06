"""
数据结构模块
导出所有数据结构定义
"""
from .common import (
    Position,
    TextRange,
    FileChange,
    TargetLocation,
    BibliographyUpdate,
    ChangeType,
    SemanticPosition,
    TextMatchPosition,
    LaTeXNode
)

__all__ = [
    "Position",
    "TextRange",
    "FileChange",
    "TargetLocation",
    "BibliographyUpdate",
    "ChangeType",
    "SemanticPosition",
    "TextMatchPosition",
    "LaTeXNode"
]
