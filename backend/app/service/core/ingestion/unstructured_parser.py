from __future__ import annotations

from typing import List
import os
from service.core.ingestion.interfaces import DocumentParser, ParsedBlock
from utils.get_logger import log


class UnstructuredParser(DocumentParser):
    """Unstructured 通用解析器。
    
    支持多种文档格式（PDF/DOCX/TXT/HTML/MD 等），作为 MinerU 失败后的通用兜底方案。
    优先使用 unstructured 库，失败时回退到简单文本读取。
    """

    def name(self) -> str:
        return "UnstructuredParser"

    def parse(self, *, file_path: str) -> List[ParsedBlock]:
        try:
            log.info(f"UnstructuredParser.start file={file_path}")
        except Exception:
            pass
        
        _, ext = os.path.splitext(file_path.lower())
        
        # 尝试使用 unstructured 库
        try:
            blocks = self._parse_with_unstructured(file_path, ext)
            if blocks:
                log.info(f"UnstructuredParser.ok blocks={len(blocks)}")
                return blocks
        except Exception as e:
            log.warning(f"UnstructuredParser.unstructured_failed err={e}")
        
        # 兜底：简单文本读取
        return self._fallback_text_read(file_path, ext)
    
    def _parse_with_unstructured(self, file_path: str, ext: str) -> List[ParsedBlock]:
        """使用 unstructured 库解析文档"""
        try:
            from unstructured.partition.auto import partition
        except ImportError:
            log.warning("UnstructuredParser: unstructured library not installed, using fallback")
            return []
        
        # partition 自动检测文件类型并解析
        elements = partition(filename=file_path)
        
        blocks: List[ParsedBlock] = []
        for i, elem in enumerate(elements):
            text = str(elem).strip()
            if not text:
                continue
            
            # 提取元数据
            metadata = {
                "parser_engine": "unstructured",
                "element_type": getattr(elem, "category", "paragraph").lower(),
            }
            
            # 尝试获取页码
            if hasattr(elem, "metadata") and elem.metadata:
                if hasattr(elem.metadata, "page_number"):
                    metadata["page"] = elem.metadata.page_number
                # 其他可能的元数据
                if hasattr(elem.metadata, "filename"):
                    metadata["source_file"] = elem.metadata.filename
            
            blocks.append(ParsedBlock(text=text, metadata=metadata))
        
        return blocks
    
    def _fallback_text_read(self, file_path: str, ext: str) -> List[ParsedBlock]:
        """兜底：简单文本读取"""
        try:
            log.info(f"UnstructuredParser.fallback file={file_path}")
            
            # TXT/MD 直接读取
            if ext in (".txt", ".md", ".markdown"):
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read().strip()
                if text:
                    return [ParsedBlock(text=text, metadata={"parser_engine": "unstructured", "note": "text_fallback"})]
            
            # PDF 使用 PyMuPDF
            if ext == ".pdf":
                try:
                    import fitz
                    with fitz.open(file_path) as doc:
                        pages_text = []
                        for i in range(min(30, len(doc))):
                            try:
                                pages_text.append(doc[i].get_text("text") or "")
                            except Exception:
                                pages_text.append("")
                        text = "\n".join(pages_text).strip()
                    if text:
                        return [ParsedBlock(text=text, metadata={"parser_engine": "unstructured", "note": "pymupdf_fallback"})]
                except Exception:
                    pass
            
            # 其他二进制文件尝试解码
            with open(file_path, "rb") as f:
                raw = f.read()
            try:
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                text = raw.decode("latin-1", errors="ignore")
            text = text.strip()
            if text:
                return [ParsedBlock(text=text, metadata={"parser_engine": "unstructured", "note": "binary_fallback"})]
        except Exception as e:
            log.error(f"UnstructuredParser.fallback_failed err={e}")
        
        return [ParsedBlock(text="", metadata={"parser_engine": "unstructured", "note": "all_failed"})]


