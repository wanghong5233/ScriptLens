"""增量 Diff 生成器，针对大文件仅返回发生变化的上下文片段。"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Dict, List, Sequence, Tuple

ELLIPSIS = "\n% --- 省略未改动内容 ---\n"
MAX_PREVIEW_CHARS = 20000
CONTEXT_OPTIONS = (120, 80, 40, 20, 10)


def generate_diff_preview(
    original_text: str,
    modified_text: str,
    *,
    max_chars: int = MAX_PREVIEW_CHARS,
) -> Tuple[str, str, bool]:
    """生成用于前端展示的增量 diff 预览。

    如果文件较小，返回完整内容；若超出阈值，则仅返回包含变更的窗口。
    返回 (preview_original, preview_modified, is_truncated)。
    """
    if len(original_text) <= max_chars and len(modified_text) <= max_chars:
        return original_text, modified_text, False

    original_lines = original_text.splitlines()
    modified_lines = modified_text.splitlines()

    matcher = SequenceMatcher(None, original_lines, modified_lines)
    opcodes = matcher.get_opcodes()

    if not opcodes:
        # 没有检测到差异，直接截断首尾
        return (
            original_text[:max_chars],
            modified_text[:max_chars],
            True,
        )

    diff_spans_original = [(i1, i2) for tag, i1, i2, _, _ in opcodes if tag != "equal"]
    diff_spans_modified = [(j1, j2) for tag, _, _, j1, j2 in opcodes if tag != "equal"]

    for context_lines in CONTEXT_OPTIONS:
        preview_original = _render_preview(original_lines, diff_spans_original, context_lines, max_chars)
        preview_modified = _render_preview(modified_lines, diff_spans_modified, context_lines, max_chars)
        if len(preview_original) <= max_chars and len(preview_modified) <= max_chars:
            return preview_original, preview_modified, True

    # 经过多次压缩仍超出限制，只保留前 max_chars 字符
    return (
        _truncate_center(original_text, max_chars),
        _truncate_center(modified_text, max_chars),
        True,
    )


def compute_line_change_stats(original_text: str, modified_text: str) -> Dict[str, int]:
    """计算行级变更统计（新增/删除）."""
    original_lines = original_text.splitlines()
    modified_lines = modified_text.splitlines()
    matcher = SequenceMatcher(None, original_lines, modified_lines)

    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in {"replace", "delete"}:
            removed += max(0, i2 - i1)
        if tag in {"replace", "insert"}:
            added += max(0, j2 - j1)
    return {"added_lines": added, "removed_lines": removed}


def _render_preview(
    lines: List[str],
    spans: Sequence[Tuple[int, int]],
    context_lines: int,
    max_chars: int,
) -> str:
    """根据差异区间和上下文行数构建预览文本。"""
    merged = _merge_spans(lines, spans, context_lines)
    if not merged:
        return "\n".join(lines[: min(len(lines), context_lines * 2)])

    parts: List[str] = []
    for start, end in merged:
        if parts:
            parts.append(ELLIPSIS)
        snippet = "\n".join(lines[start:end])
        parts.append(snippet)
        # 提前停止，避免拼接过长
        if sum(len(part) for part in parts) > max_chars:
            break
    return "".join(parts)


def _merge_spans(
    lines: List[str],
    spans: Sequence[Tuple[int, int]],
    context_lines: int,
) -> List[Tuple[int, int]]:
    expanded: List[Tuple[int, int]] = []
    total = len(lines)
    for start, end in spans:
        ctx_start = max(0, start - context_lines)
        ctx_end = min(total, end + context_lines)
        expanded.append((ctx_start, ctx_end))

    if not expanded:
        return []

    expanded.sort()
    merged: List[Tuple[int, int]] = [expanded[0]]
    for start, end in expanded[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _truncate_center(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}{ELLIPSIS}{text[-tail:]}"


