"""
编辑类工具
"""
from typing import Dict, Any, Optional, Literal, Tuple
from pathlib import Path
import asyncio
import logging
import re

from .base_tool import BaseTool, ToolResult
from .workspace_utils import (
    get_workspace_path,
    resolve_path_within_workspace,
    ensure_parent_directory,
)

logger = logging.getLogger(__name__)


def detect_document_language(content: str) -> Literal["chinese", "english", "mixed"]:
    """
    检测文档的主要语言
    
    Args:
        content: 文档内容
        
    Returns:
        "chinese": 主要是中文
        "english": 主要是英文
        "mixed": 混合语言
    """
    # 移除 LaTeX 命令和注释，只分析实际文本内容
    # 移除 LaTeX 命令（\command{...}）
    text_only = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', content)
    # 移除注释
    text_only = re.sub(r'%.*', '', text_only)
    # 移除 LaTeX 环境标记
    text_only = re.sub(r'\\begin\{[^}]+\}.*?\\end\{[^}]+\}', '', text_only, flags=re.DOTALL)
    # 移除其他 LaTeX 特殊字符
    text_only = re.sub(r'[\\{}]', '', text_only)
    
    # 统计中文字符
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text_only)
    chinese_count = len(chinese_chars)
    
    # 统计英文单词（至少2个字母）
    english_words = re.findall(r'\b[a-zA-Z]{2,}\b', text_only)
    english_count = len(english_words)
    
    # 计算总字符数（用于判断比例）
    total_chars = len(re.findall(r'[\u4e00-\u9fff]', text_only)) + len(re.findall(r'[a-zA-Z]', text_only))
    
    if total_chars == 0:
        return "english"  # 默认英文（LaTeX 命令不算）
    
    chinese_ratio = chinese_count / total_chars if total_chars > 0 else 0
    
    # 如果中文字符占比超过 30%，认为是中文文档
    if chinese_ratio > 0.3:
        return "chinese"
    # 如果英文单词数量远大于中文字符，认为是英文文档
    elif english_count > chinese_count * 3:
        return "english"
    # 否则认为是混合语言
    else:
        return "mixed"


def detect_text_language(text: str) -> Literal["chinese", "english", "mixed"]:
    """
    检测文本的主要语言（用于检查要插入的文本）
    
    Args:
        text: 要检测的文本
        
    Returns:
        "chinese": 主要是中文
        "english": 主要是英文
        "mixed": 混合语言
    """
    # 移除 LaTeX 命令和特殊字符
    text_only = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', text)
    text_only = re.sub(r'%.*', '', text_only)
    text_only = re.sub(r'[\\{}]', '', text_only)
    
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text_only)
    chinese_count = len(chinese_chars)
    
    english_words = re.findall(r'\b[a-zA-Z]{2,}\b', text_only)
    english_count = len(english_words)
    
    total_chars = len(re.findall(r'[\u4e00-\u9fff]', text_only)) + len(re.findall(r'[a-zA-Z]', text_only))
    
    if total_chars == 0:
        return "english"
    
    chinese_ratio = chinese_count / total_chars if total_chars > 0 else 0
    
    if chinese_ratio > 0.3:
        return "chinese"
    elif english_count > chinese_count * 3:
        return "english"
    else:
        return "mixed"


def check_language_consistency(
    document_content: str,
    new_text: str,
    file_path: str
) -> Tuple[bool, Optional[str]]:
    """
    检查新文本与文档的语言一致性
    
    Args:
        document_content: 文档内容
        new_text: 要插入/替换的新文本
        file_path: 文件路径（用于错误信息）
        
    Returns:
        (is_consistent, error_message)
    """
    doc_lang = detect_document_language(document_content)
    text_lang = detect_text_language(new_text)
    
    # 如果文档是英文，但新文本包含中文，则不一致
    if doc_lang == "english" and text_lang in ("chinese", "mixed"):
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', new_text)
        if chinese_chars:
            return False, (
                f"语言不一致错误：检测到文档 {file_path} 是英文文档，"
                f"但生成的内容包含中文字符。请确保生成的内容使用英文，"
                f"与文档的语言保持一致。"
            )
    
    # 如果文档是中文，但新文本主要是英文（且没有中文），则警告（但不阻止）
    # 因为中文文档中可能包含英文摘要等部分
    if doc_lang == "chinese" and text_lang == "english":
        logger.warning(
            f"语言提示：文档 {file_path} 是中文文档，但生成的内容主要是英文。"
            f"请确认这是否符合预期（如英文摘要部分）。"
        )
    
    return True, None


class RewriteSelectionTool(BaseTool):
    """
    重写指定选区工具
    用于整体替换某段文本（例如摘要、段落或句子）
    """

    def __init__(self):
        super().__init__(
            name="rewrite_selection_tool",
            description="重写文件中的指定选区。适用于根据上下文替换原有内容。"
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "目标文件路径"
                },
                "start_offset": {
                    "type": "integer",
                    "description": "选区起始字符偏移（0-based）"
                },
                "end_offset": {
                    "type": "integer",
                    "description": "选区结束字符偏移（0-based，非包含）"
                },
                "replacement_text": {
                    "type": "string",
                    "description": "替换后的文本内容"
                }
            },
            "required": ["file_path", "start_offset", "end_offset", "replacement_text"]
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any]
    ) -> ToolResult:
        file_path = parameters.get("file_path")
        start_offset = parameters.get("start_offset")
        end_offset = parameters.get("end_offset")
        replacement_text = parameters.get("replacement_text")

        if file_path is None or start_offset is None or end_offset is None or replacement_text is None:
            return ToolResult(success=False, error="缺少必需参数：file_path/start_offset/end_offset/replacement_text")

        if start_offset < 0 or end_offset < 0:
            return ToolResult(success=False, error="start_offset 和 end_offset 必须为非负整数")

        if start_offset > end_offset:
            return ToolResult(success=False, error="start_offset 不能大于 end_offset")

        try:
            workspace_path = get_workspace_path(agent_state)
            target_file = resolve_path_within_workspace(workspace_path, file_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if not target_file.exists():
            return ToolResult(success=False, error=f"文件不存在: {file_path}")

        try:
            original_content = await asyncio.to_thread(target_file.read_text, "utf-8")
        except Exception as exc:
            logger.error("读取文件失败: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"读取文件失败: {exc}")

        if end_offset > len(original_content):
            return ToolResult(success=False, error="end_offset 超出文件长度")

        # 检查语言一致性
        is_consistent, lang_error = check_language_consistency(
            original_content,
            replacement_text,
            file_path
        )
        if not is_consistent:
            return ToolResult(success=False, error=lang_error)

        new_content = original_content[:start_offset] + replacement_text + original_content[end_offset:]

        try:
            await asyncio.to_thread(target_file.write_text, new_content, "utf-8")
        except Exception as exc:
            logger.error("写入文件失败: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"写入文件失败: {exc}")

        if hasattr(agent_state, "modified_files"):
            agent_state.modified_files.add(file_path)

        logger.info(
            "RewriteSelectionTool: rewrote %s [%s:%s] (%s chars)",
            file_path,
            start_offset,
            end_offset,
            len(replacement_text)
        )

        return ToolResult(
            success=True,
            data={
                "file_path": file_path,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "replacement": replacement_text
            },
            summary=f"已重写 {file_path} 中的选区（{start_offset}-{end_offset}）"
        )


class RewriteLineRangeTool(BaseTool):
    """
    按行重写工具
    使用 1-based 行号替换文件中的指定行区间，适合先定位后精确修改。
    """

    def __init__(self):
        super().__init__(
            name="rewrite_line_range_tool",
            description=(
                "按行替换文件中的指定区间（1-based，包含边界）。"
                "推荐配合 search_codebase_tool + read_file_range_tool 使用。"
            ),
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "目标文件路径（相对工作区）",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行（1-based，包含）",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行（1-based，包含）",
                },
                "replacement_text": {
                    "type": "string",
                    "description": "替换后的文本内容（可为空字符串表示删除该区间）",
                },
                "expected_context": {
                    "type": "string",
                    "description": "可选安全校验：期望在原区间中出现的文本片段（忽略空白后比较）",
                },
            },
            "required": ["file_path", "start_line", "end_line", "replacement_text"],
        }

    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any],
    ) -> ToolResult:
        file_path = str(parameters.get("file_path") or "").strip()
        if not file_path:
            return ToolResult(success=False, error="file_path 参数不能为空")

        try:
            start_line = int(parameters.get("start_line"))
            end_line = int(parameters.get("end_line"))
        except Exception:
            return ToolResult(success=False, error="start_line / end_line 必须为整数")

        if start_line < 1 or end_line < 1:
            return ToolResult(success=False, error="start_line / end_line 必须 >= 1")
        if end_line < start_line:
            return ToolResult(success=False, error="end_line 不能小于 start_line")

        replacement_text = parameters.get("replacement_text")
        if replacement_text is None:
            return ToolResult(success=False, error="replacement_text 参数不能为空")
        replacement_text = str(replacement_text)
        expected_context = str(parameters.get("expected_context") or "").strip()

        try:
            workspace_path = get_workspace_path(agent_state)
            target_file = resolve_path_within_workspace(workspace_path, file_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        if not target_file.exists():
            return ToolResult(success=False, error=f"文件不存在: {file_path}")

        try:
            original_content = await asyncio.to_thread(target_file.read_text, "utf-8")
        except Exception as exc:
            logger.error("读取文件失败: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"读取文件失败: {exc}")

        lines = original_content.splitlines(keepends=True)
        total_lines = len(lines)
        if total_lines == 0:
            return ToolResult(success=False, error="目标文件为空，无法按行区间替换")
        if start_line > total_lines:
            return ToolResult(success=False, error=f"start_line 超出范围（总行数 {total_lines}）")

        effective_end_line = min(end_line, total_lines)
        start_idx = start_line - 1
        end_idx_exclusive = effective_end_line
        original_slice = "".join(lines[start_idx:end_idx_exclusive])

        if expected_context:
            normalized_expected = re.sub(r"\s+", " ", expected_context).strip().lower()
            normalized_original = re.sub(r"\s+", " ", original_slice).strip().lower()
            if normalized_expected and normalized_expected not in normalized_original:
                return ToolResult(
                    success=False,
                    error="expected_context 与目标行区间不匹配，已拒绝写入以避免误改",
                )

        is_consistent, lang_error = check_language_consistency(
            original_content,
            replacement_text,
            file_path,
        )
        if not is_consistent:
            return ToolResult(success=False, error=lang_error)

        replacement_lines = replacement_text.splitlines(keepends=True)
        if (
            replacement_text
            and replacement_lines
            and not replacement_lines[-1].endswith("\n")
            and end_idx_exclusive < total_lines
        ):
            # 中间区间替换时，末行未携带换行会与后续原文粘连，自动补齐。
            replacement_lines[-1] = replacement_lines[-1] + "\n"

        new_lines = lines[:start_idx] + replacement_lines + lines[end_idx_exclusive:]
        new_content = "".join(new_lines)

        try:
            await asyncio.to_thread(target_file.write_text, new_content, "utf-8")
        except Exception as exc:
            logger.error("写入文件失败: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"写入文件失败: {exc}")

        if hasattr(agent_state, "modified_files"):
            agent_state.modified_files.add(file_path)

        replacement_line_count = replacement_text.count("\n") + (1 if replacement_text else 0)
        return ToolResult(
            success=True,
            data={
                "file_path": file_path,
                "start_line": start_line,
                "end_line": effective_end_line,
                "replaced_lines": effective_end_line - start_line + 1,
                "replacement_lines": replacement_line_count,
            },
            summary=(
                f"已按行重写 {file_path} 的 L{start_line}-L{effective_end_line} "
                f"（替换 {effective_end_line - start_line + 1} 行）"
            ),
        )


class InsertTextTool(BaseTool):
    """
    插入文本工具
    在 LaTeX 文档的指定位置插入文本内容（如摘要、段落等）
    使用上下文定位，类似 Cursor 的编辑方式
    """
    
    def __init__(self):
        super().__init__(
            name="insert_text_tool",
            description=(
                "在 LaTeX 文档中插入文本内容（段落、摘要、章节等）。"
                "使用上下文定位：提供要在其后插入内容的文本片段（search_context），"
                "确保唯一匹配。例如：在 \\begin{abstract} 后插入，"
                "提供包含该标记及其前后几行的上下文。"
                "若用户要求“修改/重写原文”，请使用 insert_mode='replace' 做原位替换，而非追加。"
            )
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径（相对于工作区）"
                },
                "text_to_insert": {
                    "type": "string",
                    "description": "要插入的文本内容"
                },
                "search_context": {
                    "type": "string",
                    "description": (
                        "用于定位插入位置的上下文文本。"
                        "应包含要在其后插入内容的代码段，包括前后几行以确保唯一性。"
                        "插入位置在此上下文的末尾。"
                    )
                },
                "insert_mode": {
                    "type": "string",
                    "description": (
                        "模式：'after'（在上下文后插入，默认）、"
                        "'before'（在上下文前插入）、"
                        "'replace'（将匹配到的 search_context 整段替换为 text_to_insert）、"
                        "'replace_all'（整文件替换，不依赖 search_context，适用于 @file 整体重写）"
                    ),
                    "enum": ["after", "before", "replace", "replace_all"],
                    "default": "after"
                }
            },
            "required": ["file_path", "text_to_insert"]
        }
    
    async def execute(
        self,
        agent_state: Any,
        parameters: Dict[str, Any]
    ) -> ToolResult:
        """
        执行文本插入
        
        Args:
            parameters:
                - file_path: 文件路径
                - text_to_insert: 要插入的文本内容
                - search_context: 用于定位的上下文文本
                - insert_mode: 'after' / 'before' / 'replace' / 'replace_all'
        """
        file_path = parameters.get("file_path")
        text_to_insert = parameters.get("text_to_insert", "")
        search_context = parameters.get("search_context", "")
        insert_mode = parameters.get("insert_mode", "after")
        if insert_mode not in {"after", "before", "replace", "replace_all"}:
            insert_mode = "after"
        
        if not file_path:
            return ToolResult(
                success=False,
                error="Missing required parameter: file_path"
            )
        
        if not text_to_insert.strip():
            return ToolResult(
                success=False,
                error="text_to_insert parameter cannot be empty"
            )
        
        if insert_mode != "replace_all" and not search_context.strip():
            return ToolResult(
                success=False,
                error="search_context parameter cannot be empty. Provide context lines to locate insert position."
            )
        
        try:
            workspace_path = get_workspace_path(agent_state)
            target_file = resolve_path_within_workspace(workspace_path, file_path)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        
        if not target_file.exists():
            return ToolResult(success=False, error=f"文件不存在: {file_path}")

        # 读取文件内容以检查语言一致性
        try:
            file_content = await asyncio.to_thread(target_file.read_text, "utf-8")
        except Exception as exc:
            logger.error("读取文件失败: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"读取文件失败: {exc}")
        
        # 检查语言一致性
        is_consistent, lang_error = check_language_consistency(
            file_content,
            text_to_insert,
            file_path
        )
        if not is_consistent:
            return ToolResult(success=False, error=lang_error)

        try:
            result = await asyncio.to_thread(
                self._insert_text_with_context,
                target_file,
                text_to_insert,
                search_context,
                insert_mode
            )
            
            if result["success"]:
                # 标记文件已修改（用于生成 diff）
                agent_state.modified_files.add(str(file_path))
                operation = str(result.get("operation") or insert_mode)
                summary_action = "替换" if operation in {"replace", "replace_all", "replace_all_fallback"} else "插入"
                
                return ToolResult(
                    success=True,
                    data={
                        "file_path": file_path,
                        "inserted_lines": result["inserted_lines"],
                        "insert_position": result["insert_line"],
                        "operation": operation,
                        "match_mode": result.get("match_mode"),
                    },
                    summary=f"在 {file_path} 成功{summary_action} {result['inserted_lines']} 行文本（位置：第 {result['insert_line']} 行附近）"
                )
            else:
                return ToolResult(success=False, error=result["error"])
        
        except Exception as e:
            logger.error(f"Insert text failed: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))
    
    def _insert_text_with_context(
        self,
        file_path: Path,
        text_to_insert: str,
        search_context: str,
        insert_mode: str = "after"
    ) -> Dict[str, Any]:
        """
        使用上下文定位并插入文本（类似 Cursor 的编辑方式）
        
        Args:
            file_path: 文件路径
            text_to_insert: 要插入的文本
            search_context: 用于定位的上下文文本
            insert_mode: 'after' / 'before' / 'replace' / 'replace_all'
        
        Returns:
            {success: bool, insert_line: int, inserted_lines: int, error: str}
        """
        try:
            # 读取原文件内容
            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
            
            # 查找上下文在文件中的位置
            operation = insert_mode
            if operation == "replace_all":
                insert_position = 0
                text_to_insert_formatted = text_to_insert
                new_content = text_to_insert_formatted
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                inserted_lines = text_to_insert.count('\n') + 1
                logger.info(
                    f"InsertTextTool {operation}: replaced whole file with {inserted_lines} lines in {file_path}"
                )
                return {
                    "success": True,
                    "insert_line": 1,
                    "inserted_lines": inserted_lines,
                    "operation": operation,
                    "match_mode": "replace_all",
                }

            def _sanitize_candidate_context(raw: str) -> str:
                text = str(raw or "")
                if not text:
                    return ""
                cleaned_lines: list[str] = []
                for line in text.splitlines():
                    normalized_line = line.strip("\r")
                    if not normalized_line:
                        continue
                    if normalized_line.startswith("```"):
                        continue
                    if re.match(r"^\[(HEAD|TAIL|KEYWORD_HITS|HIT)\b", normalized_line):
                        continue
                    normalized_line = re.sub(r"^\s*L\d+\s*:\s?", "", normalized_line)
                    cleaned_lines.append(normalized_line)
                return "\n".join(cleaned_lines).strip()

            context_candidates: list[str] = []
            raw_context = str(search_context or "")
            if raw_context.strip():
                context_candidates.append(raw_context)
            sanitized_context = _sanitize_candidate_context(raw_context)
            if sanitized_context and sanitized_context not in context_candidates:
                context_candidates.append(sanitized_context)
            normalized_space_context = re.sub(r"\s+", " ", sanitized_context).strip()
            if normalized_space_context and normalized_space_context not in context_candidates:
                context_candidates.append(normalized_space_context)

            context_index = -1
            context_end_index = -1
            matched_context = ""
            match_mode = "not_found"

            def _find_unique_exact(haystack: str, needle: str) -> Tuple[int, int]:
                if not needle:
                    return -1, -1
                first = haystack.find(needle)
                if first == -1:
                    return -1, -1
                second = haystack.find(needle, first + 1)
                if second != -1:
                    return -2, -2
                return first, first + len(needle)

            for candidate in context_candidates:
                idx, end_idx = _find_unique_exact(file_content, candidate)
                if idx == -2:
                    return {
                        "success": False,
                        "error": (
                            "找到多个匹配的上下文（不唯一）。"
                            "请提供更多的上下文行以确保唯一匹配。"
                        )
                    }
                if idx >= 0:
                    context_index = idx
                    context_end_index = end_idx
                    matched_context = candidate
                    match_mode = "exact"
                    break
            if context_index >= 0 and context_end_index < 0:
                context_end_index = context_index + len(matched_context)

            def _normalize_with_map(value: str) -> Tuple[str, list[int]]:
                normalized_chars: list[str] = []
                index_map: list[int] = []
                prev_space = True
                for idx, ch in enumerate(value):
                    if ch.isspace():
                        if not prev_space:
                            normalized_chars.append(" ")
                            index_map.append(idx)
                        prev_space = True
                        continue
                    normalized_chars.append(ch.lower())
                    index_map.append(idx)
                    prev_space = False
                if normalized_chars and normalized_chars[-1] == " ":
                    normalized_chars.pop()
                    index_map.pop()
                return "".join(normalized_chars), index_map

            # 回退匹配：忽略大小写与空白差异（避免模型复述上下文时微小偏差导致定位失败）
            if context_index == -1:
                normalized_file, file_map = _normalize_with_map(file_content)
                for candidate in context_candidates:
                    normalized_context, _ = _normalize_with_map(candidate)
                    if not normalized_context:
                        continue
                    normalized_index = normalized_file.find(normalized_context)
                    if normalized_index == -1:
                        continue
                    second_normalized = normalized_file.find(normalized_context, normalized_index + 1)
                    if second_normalized != -1:
                        return {
                            "success": False,
                            "error": (
                                "找到多个近似匹配的上下文（忽略空白后不唯一）。"
                                "请提供更多上下文行或更长的唯一片段。"
                            )
                        }
                    context_index = file_map[normalized_index]
                    normalized_end = normalized_index + len(normalized_context) - 1
                    context_end_index = file_map[normalized_end] + 1
                    match_mode = "normalized_whitespace"
                    matched_context = candidate
                    break

            if context_index == -1:
                hint_from_file_excerpt = bool(
                    re.search(r"\bL\d+\s*:", raw_context)
                    or "[HEAD" in raw_context
                    or "[TAIL" in raw_context
                    or "[HIT" in raw_context
                )
                likely_full_rewrite = len(text_to_insert.strip()) >= max(180, int(max(len(file_content), 1) * 0.25))
                if operation == "replace" and hint_from_file_excerpt and likely_full_rewrite:
                    # 容错回退：模型常把 @file 注入的编号/分段标记复述为 search_context，精确定位失败时退化为整文件替换。
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(text_to_insert)
                    inserted_lines = text_to_insert.count('\n') + 1
                    logger.info(
                        "InsertTextTool replace fallback -> replace_all: %s lines in %s",
                        inserted_lines,
                        file_path,
                    )
                    return {
                        "success": True,
                        "insert_line": 1,
                        "inserted_lines": inserted_lines,
                        "operation": "replace_all_fallback",
                        "match_mode": "fallback_replace_all_on_miss",
                    }
                return {
                    "success": False,
                    "error": (
                        "未找到匹配的上下文。请提供更精确的上下文文本，"
                        "包括要插入位置前后的几行代码。"
                    )
                }

            # 检查是否有多个匹配（上下文不唯一）
            if match_mode == "exact":
                second_match = file_content.find(search_context, context_index + 1)
                if second_match != -1:
                    return {
                        "success": False,
                        "error": (
                            "找到多个匹配的上下文（不唯一）。"
                            "请提供更多的上下文行以确保唯一匹配。"
                        )
                    }
            
            if operation == "replace":
                # 原位替换匹配上下文，适用于“修改/重写”类需求。
                insert_position = context_index
                text_to_insert_formatted = text_to_insert
                new_content = (
                    file_content[:context_index]
                    + text_to_insert_formatted
                    + file_content[context_end_index:]
                )
            else:
                # 确定插入位置
                if operation == "after":
                    # 在上下文之后插入
                    insert_position = context_index + len(search_context)
                else:  # before
                    # 在上下文之前插入
                    insert_position = context_index

                # 确保插入的文本前后有适当的换行符
                text_to_insert_formatted = text_to_insert
                if not text_to_insert.startswith('\n') and insert_position > 0:
                    text_to_insert_formatted = '\n' + text_to_insert_formatted
                if not text_to_insert.endswith('\n'):
                    text_to_insert_formatted = text_to_insert_formatted + '\n'

                # 执行插入
                new_content = (
                    file_content[:insert_position] +
                    text_to_insert_formatted +
                    file_content[insert_position:]
                )
            
            # 写回文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            
            # 计算插入位置的行号（用于日志）
            insert_line = file_content[:insert_position].count('\n') + 1
            inserted_lines = text_to_insert.count('\n') + 1
            
            logger.info(
                f"InsertTextTool {operation}: {inserted_lines} lines at line ~{insert_line} in {file_path}"
            )
            
            return {
                "success": True,
                "insert_line": insert_line,
                "inserted_lines": inserted_lines,
                "operation": operation,
                "match_mode": match_mode,
            }
        
        except Exception as e:
            logger.error(f"Failed to insert text with context: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

