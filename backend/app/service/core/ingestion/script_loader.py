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
    return [p.text.strip() for p in doc.paragraphs if p.text.strip()]


def _load_pdf(path: Path) -> List[str]:
    import fitz  # pymupdf, lazy import

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
