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


class ScannedPdfError(ValueError):
    """PDF 是扫描件（图片）/ 没有可提取文本时抛出。

    上游会把 .args[0] 直接当作 user-facing failure_reason 落 scripts 表，
    所以这条消息必须是给"非工程"用户看的中文 + actionable。
    """


class EmptyScriptError(ValueError):
    """非扫描件但解析后无任何段落（损坏 / 加密 / 空文档）时抛出。"""


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
    total_pages = 0
    pages_with_images = 0
    pdf = fitz.open(str(path))
    try:
        for page in pdf:
            total_pages += 1
            text = page.get_text("text") or ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    out.append(line)
            # 用 page.get_images() 而不是 page.images（pypdf 风格）；fitz 这个方法
            # 返回当前页引用的图片 xref 列表，是判断扫描件最直接的信号。
            try:
                if page.get_images(full=False):
                    pages_with_images += 1
            except Exception:
                # 损坏页只忽略，不影响其余页统计
                pass
    finally:
        pdf.close()

    if not out:
        # 0 文本：决定是"扫描件"还是"空/损坏 PDF"。扫描件的判定阈值：≥80% 的页
        # 有图片资源 —— 文本型 PDF 偶尔嵌一两张配图不会触发；25/25 全图必定触发。
        if total_pages > 0 and pages_with_images >= total_pages * 0.8:
            raise ScannedPdfError(
                "上传的 PDF 是扫描件（每页都是图片），无法直接提取剧本文字。"
                "请将剧本另存为 .docx / .txt / .md 后重新上传；"
                "或先用 OCR 工具（ABBYY / Adobe Acrobat / 白描）把扫描件转成可复制文本后再上传。"
            )
        raise EmptyScriptError(
            "PDF 解析后没有任何文字（可能是空文档、加密文档或文件损坏）。"
            "请确认文件可在阅读器中正常显示文字后再重新上传。"
        )
    return out


def _load_text(path: Path) -> List[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in raw.splitlines() if line.strip()]
