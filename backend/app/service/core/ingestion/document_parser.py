from __future__ import annotations

from typing import List
import os

from service.core.ingestion.interfaces import ParsedBlock, DocumentParser
from utils.get_logger import log


class LightweightDocumentParser(DocumentParser):
    """ScriptLens MVP 轻量解析器：

    - .txt / .md：直接读取
    - .pdf：用 PyMuPDF 提取文本
    - .docx：用 python-docx 提取段落（剧本场景的主路径）
    - 其它：返回空块占位
    """

    MAX_PDF_PAGES = 200  # 短剧 PDF 最多约 160 页，留点 buffer

    def parse(self, *, file_path: str) -> List[ParsedBlock]:
        _, ext = os.path.splitext(file_path.lower())
        log.info(f"ParseEntry: file={file_path} ext={ext}")

        if ext in {".txt", ".md"}:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return [ParsedBlock(text=f.read(), metadata={"page": 1})]

        if ext == ".pdf":
            return self._parse_pdf(file_path)

        if ext == ".docx":
            return self._parse_docx(file_path)

        log.warning(f"LightweightDocumentParser: unsupported ext {ext} for {file_path}")
        return [ParsedBlock(text="", metadata={"note": f"unsupported_ext:{ext}"})]

    def _parse_pdf(self, file_path: str) -> List[ParsedBlock]:
        import fitz  # PyMuPDF

        with fitz.open(file_path) as doc:
            page_count = min(self.MAX_PDF_PAGES, len(doc))
            blocks: List[ParsedBlock] = []
            for i in range(page_count):
                text = doc[i].get_text("text") or ""
                if text.strip():
                    blocks.append(ParsedBlock(text=text, metadata={"page": i + 1}))

        if not blocks:
            return [ParsedBlock(text="", metadata={"note": "pdf_no_text_layer"})]
        log.info(f"ParseStats: ext=.pdf pages={page_count} blocks={len(blocks)}")
        return blocks

    def _parse_docx(self, file_path: str) -> List[ParsedBlock]:
        from docx import Document as DocxDocument

        doc = DocxDocument(file_path)
        blocks: List[ParsedBlock] = []
        for idx, para in enumerate(doc.paragraphs):
            text = (para.text or "").strip()
            if text:
                blocks.append(ParsedBlock(text=text, metadata={"para_index": idx}))

        if not blocks:
            return [ParsedBlock(text="", metadata={"note": "docx_no_paragraphs"})]
        log.info(f"ParseStats: ext=.docx paragraphs={len(blocks)}")
        return blocks
