"""Document parse preview service."""

from __future__ import annotations

from collections import Counter
import logging
import os
from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.config import settings
from exceptions.base import PermissionDeniedException, ResourceNotFoundException
from models.user import User
from schemas.document import (
    DocumentParseBlock,
    DocumentParsePreviewResponse,
    DocumentParseStage,
    DocumentParseStats,
)
from service import document_service, knowledgebase_service
from service.core.ingestion.chunker import RecursiveCharacterChunker
from service.core.ingestion.constants import is_multimodal_metadata
from service.core.ingestion.parser_orchestrator import ParserOrchestrator
from service.core.ingestion.structured_doc_builder import StructuredDocument, StructuredDocumentBuilder


class ParsePreviewService:
    """Build a multi-stage parse preview for a document."""

    def __init__(self, *, db: Session, current_user: User) -> None:
        """Initialize the parse preview service.

        Args:
            db (Session): SQLAlchemy session.
            current_user (User): Authenticated user.
        """
        self.db = db
        self.current_user = current_user
        self.logger = logging.getLogger(__name__)

    def build_preview(self, *, kb_id: int, doc_id: int) -> DocumentParsePreviewResponse:
        """Build parse preview output for a document.

        Args:
            kb_id (int): Knowledge base id.
            doc_id (int): Document id.

        Returns:
            DocumentParsePreviewResponse: Preview response.
        """
        try:
            knowledgebase_service.get_kb_by_id(
                db=self.db, kb_id=kb_id, user_id=self.current_user.id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        try:
            doc = document_service.get_document_by_id(
                self.db, doc_id, self.current_user.id, kb_id
            )
        except (ResourceNotFoundException, PermissionDeniedException) as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        file_path = doc.local_pdf_path
        if not file_path:
            raise HTTPException(status_code=404, detail="文档未关联本地文件，无法解析。")
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")

        orchestrator = ParserOrchestrator()
        try:
            blocks = orchestrator.parse(file_path=file_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"解析失败: {exc}") from exc

        parser_blocks = self._convert_parsed_blocks(blocks)
        parser_stage = self._build_stage(
            key="parser",
            title="解析输出",
            description="解析器（MinerU/Unstructured/PyMuPDF）返回的原始块。",
            blocks=parser_blocks,
        )

        structured_builder = StructuredDocumentBuilder()
        structured_doc = structured_builder.build(document=doc, mineru_blocks=blocks)
        structured_blocks = self._convert_structured_blocks(structured_doc)
        structured_stage = self._build_stage(
            key="structured",
            title="结构化输出",
            description="Grobid + MinerU 对齐后的结构块（带章节、页码和 bbox 信息）。",
            blocks=structured_blocks,
        )

        structured_parsed_blocks = structured_doc.to_parsed_blocks()
        chunker = RecursiveCharacterChunker()
        try:
            chunked_blocks_raw = chunker.chunk(blocks=structured_parsed_blocks)
        except Exception as exc:
            self.logger.error("Chunker execution failed for doc_id=%s: %s", doc_id, exc)
            chunked_blocks_raw = []
        chunker_blocks = self._convert_parsed_blocks(chunked_blocks_raw)
        chunker_stage = self._build_stage(
            key="chunker",
            title="分块后输出",
            description="运行 Chunker/语义合并后的片段（入库前）。",
            blocks=chunker_blocks,
        )

        indexed_blocks = self._load_indexed_blocks(kb_id, doc_id)
        indexed_stage = None
        if indexed_blocks:
            indexed_stage = self._build_stage(
                key="indexed",
                title="实际入库 Chunk",
                description="从向量库读取的已入库 chunk 内容（排序按 chunk_index）。",
                blocks=indexed_blocks,
            )

        stages: List[DocumentParseStage] = [parser_stage, structured_stage, chunker_stage]
        if indexed_stage:
            stages.append(indexed_stage)

        parser_order = [name for name in (orchestrator.order or []) if name]
        primary_stats = (
            stages[0].stats
            if stages
            else DocumentParseStats(
                total_blocks=0,
                nonempty_blocks=0,
                total_chars=0,
                element_types={},
                parser_engines={},
            )
        )
        primary_blocks = stages[0].blocks if stages else []

        return DocumentParsePreviewResponse(
            document_id=doc.id,
            knowledge_base_id=doc.knowledge_base_id,
            filename=os.path.basename(file_path),
            parser_order=parser_order,
            stages=stages,
            stats=primary_stats,
            blocks=primary_blocks,
        )

    def _convert_parsed_blocks(self, parsed_blocks) -> List[DocumentParseBlock]:
        converted: List[DocumentParseBlock] = []
        for idx, block in enumerate(parsed_blocks, start=1):
            metadata = dict(block.metadata or {})
            converted.append(
                DocumentParseBlock(
                    index=idx,
                    text=block.text or "",
                    element_type=metadata.get("element_type"),
                    page=metadata.get("page"),
                    metadata=metadata,
                )
            )
        return converted

    def _stats_from_blocks(self, blocks: List[DocumentParseBlock]) -> DocumentParseStats:
        element_counter: Counter[str] = Counter()
        parser_counter: Counter[str] = Counter()
        nonempty_blocks = 0
        total_chars = 0
        for block in blocks:
            text = block.text or ""
            if text.strip():
                nonempty_blocks += 1
                total_chars += len(text)
            element_counter[str(block.element_type or "unknown")] += 1
            parser_counter[str(block.metadata.get("parser_engine") or "unknown")] += 1
        return DocumentParseStats(
            total_blocks=len(blocks),
            nonempty_blocks=nonempty_blocks,
            total_chars=total_chars,
            element_types=dict(element_counter),
            parser_engines=dict(parser_counter),
        )

    def _build_stage(
        self,
        *,
        key: str,
        title: str,
        description: str | None,
        blocks: List[DocumentParseBlock],
    ) -> DocumentParseStage:
        return DocumentParseStage(
            key=key,
            title=title,
            description=description,
            stats=self._stats_from_blocks(blocks),
            blocks=blocks,
        )

    def _convert_structured_blocks(
        self, structured_doc: StructuredDocument
    ) -> List[DocumentParseBlock]:
        converted: List[DocumentParseBlock] = []
        for idx, block in enumerate(structured_doc.blocks, start=1):
            metadata = dict(block.metadata or {})
            metadata.update(
                {
                    "logical_type": block.logical_type,
                    "structure_path": block.structure_path,
                    "structure_level": block.level,
                    "structure_title": block.title,
                }
            )
            pages = metadata.get("page_range") or []
            page_val = pages[0] if pages else None
            original_element = metadata.get("element_type")
            if original_element != block.logical_type:
                if original_element:
                    metadata.setdefault("original_element_type", original_element)
            metadata["element_type"] = block.logical_type
            element_type = metadata["element_type"]
            converted.append(
                DocumentParseBlock(
                    index=idx,
                    text=block.text,
                    element_type=element_type or block.logical_type,
                    page=page_val,
                    metadata=metadata,
                )
            )
        return converted

    def _load_indexed_blocks(self, kb: int, document: int) -> List[DocumentParseBlock]:
        size = int(getattr(settings, "SM_DEBUG_MAX_CHUNKS", 2000) or 2000)
        vector_store = str(getattr(settings, "SM_VECTOR_STORE", "pgvector") or "pgvector").strip().lower()
        if vector_store == "pgvector":
            return self._load_indexed_blocks_from_pgvector(kb=kb, document=document, size=size)
        return self._load_indexed_blocks_from_es(kb=kb, document=document, size=size)

    def _load_indexed_blocks_from_pgvector(self, kb: int, document: int, size: int) -> List[DocumentParseBlock]:
        try:
            from service.core.ingestion.pgvector_writer import PgVectorChunkWriter

            rows = PgVectorChunkWriter().fetch_document_chunks(
                kb_id=kb,
                document_id=document,
                index_name=settings.ES_DEFAULT_INDEX,
                limit=size,
            )
        except Exception as exc:
            self.logger.error("pgvector query failed for chunk preview doc=%s: %s", document, exc)
            return []

        blocks: List[DocumentParseBlock] = []
        for idx, row in enumerate(rows, start=1):
            metadata = dict(row.get("metadata") or {})
            metadata["chunk_id"] = row.get("chunk_id")
            if (not settings.SM_ENABLE_MULTIMODAL_CHUNKS) and is_multimodal_metadata(metadata):
                continue
            if not metadata.get("element_type") and metadata.get("logical_type"):
                metadata["element_type"] = metadata["logical_type"]
            metadata = {k: v for k, v in metadata.items() if v is not None}
            page_num = self._optional_int(metadata.get("page"))
            blocks.append(
                DocumentParseBlock(
                    index=idx,
                    text=row.get("text") or "",
                    element_type=metadata.get("element_type"),
                    page=page_num,
                    metadata=metadata,
                )
            )
        return blocks

    def _load_indexed_blocks_from_es(self, kb: int, document: int, size: int) -> List[DocumentParseBlock]:
        try:
            from service.core.rag.utils.es_conn import ESConnection

            es = ESConnection()
        except Exception as exc:
            self.logger.error("Failed to init ESConnection for chunk preview: %s", exc)
            return []
        query = {
            "size": size,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"kb_id": str(kb)}},
                        {"term": {"document_id": str(document)}},
                    ]
                }
            },
            "sort": [
                {"chunk_index": {"order": "asc", "missing": "_first", "unmapped_type": "integer"}},
                {"_doc": {"order": "asc"}},
            ],
            "_source": {"excludes": ["vector"]},
        }
        try:
            res = es.es.search(index=settings.ES_DEFAULT_INDEX, body=query)
        except Exception as exc:
            self.logger.error("ES search failed for chunk preview doc=%s: %s", document, exc)
            return []
        hits = res.get("hits", {}).get("hits", [])
        blocks: List[DocumentParseBlock] = []
        for idx, hit in enumerate(hits, start=1):
            source = hit.get("_source", {}) or {}
            metadata = {k: v for k, v in source.items() if k not in {"text", "vector"}}
            metadata["chunk_id"] = hit.get("_id")
            if (not settings.SM_ENABLE_MULTIMODAL_CHUNKS) and is_multimodal_metadata(metadata):
                continue
            if not metadata.get("element_type") and metadata.get("logical_type"):
                metadata["element_type"] = metadata["logical_type"]
            metadata = {k: v for k, v in metadata.items() if v is not None}
            page_val = source.get("page")
            page_num = None
            try:
                if page_val is not None:
                    page_num = int(page_val)
            except (TypeError, ValueError):
                page_num = None
            blocks.append(
                DocumentParseBlock(
                    index=idx,
                    text=source.get("text") or "",
                    element_type=source.get("element_type"),
                    page=page_num,
                    metadata=metadata,
                )
            )
        return blocks

    def _optional_int(self, value) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None
