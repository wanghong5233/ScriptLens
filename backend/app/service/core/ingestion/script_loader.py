"""短剧文件加载器。

输入：本地文件路径（docx / pdf / txt / md）
输出：清洗后的段落列表（已剔除空白行）

不做语义切分，仅做「字节 → 段落 list」的最低层职责。语义切分由
`script_segmenter.segment_script` 负责。

`.doc` 不支持，需要用户预先转为 `.docx`（PRD 决策）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class UnsupportedScriptFormatError(ValueError):
    """文件后缀不被支持时抛出。"""


def load_script_paragraphs(path: Path) -> List[str]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _load_docx(path)
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix in (".txt", ".md"):
        return _load_text(path)
    if suffix == ".doc":
        raise UnsupportedScriptFormatError(
            ".doc 格式不支持，请先在 Office/WPS 中另存为 .docx 后重新上传"
        )
    raise UnsupportedScriptFormatError(f"未知文件类型：{suffix}")


def _load_docx(path: Path) -> List[str]:
    from docx import Document  # python-docx, lazy import

    doc = Document(str(path))
    out: List[str] = []
    for p in doc.paragraphs:
        # docx 段落内的软换行 <w:br/> 在 python-docx 里会以 \n 出现在 p.text。
        # 例如真实剧本里见过 '第82集\n82-1 天一阁门口 日外' 写在同一段，
        # 此处必须按行拆开，否则集号头会被场号头粘连，导致后续切分丢集。
        for line in p.text.splitlines():
            line = line.strip()
            if line:
                out.append(line)
    return out


def _load_pdf(path: Path) -> List[str]:
    import fitz  # pymupdf, lazy import

    # Some real scripts contain malformed PDF resources. PyMuPDF can still
    # extract text, but by default it floods stderr with repeated MuPDF warnings.
    # Suppress those parser diagnostics here; actual extraction failures still
    # raise exceptions from fitz.open/get_text.
    tools = getattr(fitz, "TOOLS", None)
    if tools is not None and hasattr(tools, "mupdf_display_errors"):
        tools.mupdf_display_errors(False)
    if tools is not None and hasattr(tools, "mupdf_display_warnings"):
        tools.mupdf_display_warnings(False)

    out: List[str] = []
    pdf = fitz.open(str(path))
    try:
        for page in pdf:
            text = page.get_text("text") or ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    out.append(line)
    finally:
        pdf.close()
    return out


def _load_text(path: Path) -> List[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in raw.splitlines() if line.strip()]
