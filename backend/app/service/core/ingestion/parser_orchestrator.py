from __future__ import annotations

from typing import List
import requests

from utils.get_logger import log
from core.config import settings
from service.core.ingestion.interfaces import DocumentParser, ParsedBlock
from service.core.ingestion.document_parser import LightweightDocumentParser
from service.core.ingestion.remote_parsers import LlamaParseParser, UnstructuredApiParser
from service.core.ingestion.unstructured_parser import UnstructuredParser


def _build_parser(name: str) -> DocumentParser:
    key = name.strip().lower()
    if key in {"deepdoc", "pymupdf", "mineru"}:
        # ScriptLens MVP 不使用 deepdoc / MinerU；保留名称兼容，回退为轻量解析器
        return LightweightDocumentParser()
    if key == "unstructured":
        return UnstructuredParser()
    if key == "unstructured_api":
        return UnstructuredApiParser()
    if key == "llamaparse":
        return LlamaParseParser()
    return LightweightDocumentParser()


def _skip_reason(name: str) -> str | None:
    key = name.strip().lower()
    if key == "unstructured_api" and not (settings.SM_UNSTRUCTURED_API_KEY or "").strip():
        return "missing SM_UNSTRUCTURED_API_KEY"
    if key == "llamaparse" and not (settings.SM_LLAMA_PARSE_API_KEY or "").strip():
        return "missing SM_LLAMA_PARSE_API_KEY"
    return None


def _is_strict_remote_parser(name: str) -> bool:
    if not getattr(settings, "SM_REMOTE_PARSER_STRICT_FAIL", True):
        return False
    key = name.strip().lower()
    if key == "unstructured_api":
        return bool((settings.SM_UNSTRUCTURED_API_KEY or "").strip())
    if key == "llamaparse":
        return bool((settings.SM_LLAMA_PARSE_API_KEY or "").strip())
    return False


def _validate_remote_metadata(parser_name: str, blocks: List[ParsedBlock]) -> None:
    """Quality-gate a remote parser's output.

    Industry experience: even high-quality OCR/parsing services emit a small
    fraction of fragments (headers, footers, form-feed marks, isolated page
    numbers) that lack bbox or page anchors. all-or-nothing rejection turns a
    99% successful parse into a hard failure, which is wrong. We instead apply
    a configurable tolerance ratio: above the threshold the call is rejected
    (and the orchestrator falls through to the next parser); below it the
    parse is accepted and the offending blocks simply degrade to "no anchor"
    (the downstream chunker / UI handle missing bbox by falling back to
    page-level highlighting).
    """

    text_blocks = [block for block in blocks if (block.text or "").strip()]
    total = len(text_blocks)
    if total == 0:
        raise RuntimeError(f"{parser_name} returned no text blocks")

    require_page = getattr(settings, "SM_REMOTE_PARSER_REQUIRE_PAGE", True)
    require_bbox = getattr(settings, "SM_REMOTE_PARSER_REQUIRE_BBOX", True)
    max_missing_page_ratio = float(
        getattr(settings, "SM_REMOTE_PARSER_MAX_MISSING_PAGE_RATIO", 0.02)
    )
    max_missing_bbox_ratio = float(
        getattr(settings, "SM_REMOTE_PARSER_MAX_MISSING_BBOX_RATIO", 0.05)
    )

    missing_page = sum(
        1 for block in text_blocks
        if (block.metadata or {}).get("page") is None
        and not (block.metadata or {}).get("page_range")
    )
    missing_bbox = sum(
        1 for block in text_blocks
        if not ((block.metadata or {}).get("bbox_list") or (block.metadata or {}).get("bbox"))
    )

    page_ratio = missing_page / total
    bbox_ratio = missing_bbox / total
    log.info(
        f"[REMOTE_PARSER_QUALITY] {parser_name} blocks={total} "
        f"missing_page={missing_page} ({page_ratio:.1%}) "
        f"missing_bbox={missing_bbox} ({bbox_ratio:.1%}) "
        f"tolerance(page<={max_missing_page_ratio:.1%}, bbox<={max_missing_bbox_ratio:.1%})"
    )

    if require_page and page_ratio > max_missing_page_ratio:
        raise RuntimeError(
            f"{parser_name} quality gate failed: {missing_page}/{total} "
            f"({page_ratio:.1%}) text blocks missing page anchor "
            f"(tolerance {max_missing_page_ratio:.1%})"
        )

    if require_bbox and bbox_ratio > max_missing_bbox_ratio:
        raise RuntimeError(
            f"{parser_name} quality gate failed: {missing_bbox}/{total} "
            f"({bbox_ratio:.1%}) text blocks missing bbox anchor "
            f"(tolerance {max_missing_bbox_ratio:.1%})"
        )


class ParserOrchestrator:
    """按顺序尝试多种解析器，返回首个非空结果。
    顺序由 settings.SM_PARSER_ORDER 控制。
    """

    def __init__(self, order: str | None = None) -> None:
        self.order = (order or settings.SM_PARSER_ORDER or "").split(",")

    def parse(self, *, file_path: str) -> List[ParsedBlock]:
        log.info(f"[PARSER_ORCHESTRATOR_START] file={file_path} order={','.join(self.order)}")
        last_err: Exception | None = None
        for idx, name in enumerate(self.order, 1):
            skip_reason = _skip_reason(name)
            if skip_reason:
                log.info(f"[PARSER_SKIP_{idx}] {name.strip()} reason={skip_reason}")
                continue
            parser = _build_parser(name)
            try:
                log.info(f"[PARSER_TRY_{idx}] {parser.__class__.__name__}")
                blocks = parser.parse(file_path=file_path)
                if any((b.text or "").strip() for b in blocks):
                    if _is_strict_remote_parser(name):
                        _validate_remote_metadata(parser.__class__.__name__, blocks)
                    log.info(f"[PARSER_SUCCESS] {parser.__class__.__name__} blocks={len(blocks)}")
                    return blocks
                log.warning(f"[PARSER_EMPTY] {parser.__class__.__name__}")
                if _is_strict_remote_parser(name):
                    raise RuntimeError(f"{parser.__class__.__name__} returned empty parse result")
            except (RuntimeError, TimeoutError, ValueError, requests.RequestException, OSError) as e:
                last_err = e
                log.error(f"[PARSER_FAIL] {parser.__class__.__name__} err={e}")
                if _is_strict_remote_parser(name):
                    raise RuntimeError(
                        f"{parser.__class__.__name__} failed in strict remote parser mode: {e}"
                    ) from e
                continue
        if last_err:
            log.error(f"[PARSER_ALL_FAILED] last_err={last_err}")
        # 如果所有解析器都失败，抛出异常而不是静默兜底
        raise RuntimeError(f"所有解析器失败。最后错误: {last_err}")


