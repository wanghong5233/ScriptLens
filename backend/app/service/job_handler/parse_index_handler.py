from __future__ import annotations
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from service.job_handler.interfaces import BaseJobHandler, JobResult
from utils.get_logger import log
from service.core.ingestion.parser_orchestrator import ParserOrchestrator
from service.core.ingestion.chunker import RecursiveCharacterChunker, post_filter_chunks_for_embedding
from service.core.ingestion.interfaces import ParsedBlock
from core.config import settings
from service.core.ingestion.embedder import SimpleAPIEmbedder
from service.core.ingestion.indexer import ESIndexer
from service.core.rag.providers.registry import resolve_provider
from service import knowledgebase_service
from service.core.ingestion.metadata_extractor import DefaultMetadataExtractor
from service.core.ingestion.structured_doc_builder import StructuredDocumentBuilder
from service import document_service
from service.document_lifecycle import (
    count_chunks,
    mark_failed,
    mark_parsing,
    mark_ready,
)

class ParseIndexHandler(BaseJobHandler):
    def run(self, *, db, user_id: int, kb_id: int, payload: Dict[str, Any]) -> JobResult:
        doc_ids = (payload or {}).get("docs", [])
        # ParseIndex is the canonical "to ready" handler; its success count
        # must match documents.processing_status='ready' for these doc_ids.
        result = JobResult(
            total=len(doc_ids),
            touched_doc_ids=list(doc_ids),
            reconcile_with_lifecycle=True,
        )
        # 使用全局 loguru，保证输出格式一致

        orchestrator = ParserOrchestrator()
        try:
            log.info(f"ParserOrchestratorLoaded: order={','.join(orchestrator.order)}")
        except Exception:
            pass
        chunker = RecursiveCharacterChunker()
        embedder = SimpleAPIEmbedder()
        indexer = ESIndexer()
        metadata_extractor = DefaultMetadataExtractor()
        structured_builder = StructuredDocumentBuilder()

        session_index = None
        try:
            sess_id = (payload or {}).get("sessionId")
            if sess_id:
                session_index = f"sm_sess_{sess_id}"
        except Exception:
            session_index = None

        kb_provider = None
        kb_config = None
        try:
            kb_obj = knowledgebase_service.get_kb_by_id(db, kb_id, user_id)
            kb_provider = resolve_provider(getattr(kb_obj, "rag_provider", None))
            kb_config = getattr(kb_obj, "rag_config", None)
        except Exception:
            kb_provider = None
            kb_config = None

        enable_multimodal = bool(settings.SM_ENABLE_MULTIMODAL_CHUNKS)
        if isinstance(kb_config, dict):
            enable_multimodal = bool(kb_config.get("enable_multimodal"))
        if str(kb_provider or "").lower() in {"multimodal_graph"}:
            enable_multimodal = True

        for doc_id in doc_ids:
            doc = None
            current_stage = "load"
            try:
                doc_started_at = time.perf_counter()
                doc = document_service.get_document_by_id(db, doc_id, user_id, kb_id)
                if not doc.local_pdf_path or not os.path.exists(doc.local_pdf_path):
                    raise Exception("local file not found")

                # Transition to 'parsing' so the UI/job-status reflects work in
                # progress and so downstream readers can distinguish between
                # "queued" and "actually being processed".
                mark_parsing(db, doc)

                # 解析阶段 - 详细日志
                current_stage = "parse"
                try:
                    stage_started_at = time.perf_counter()
                    log.info(f"[PARSE_START] doc_id={doc_id} file={doc.local_pdf_path} kb_id={kb_id}")
                    blocks = orchestrator.parse(file_path=doc.local_pdf_path)
                    
                    # 统计解析结果
                    total_blocks = len(blocks)
                    nonempty_blocks = sum(1 for b in blocks if (b.text or "").strip())
                    total_chars = sum(len((b.text or "").strip()) for b in blocks)
                    
                    # 统计 element_type 分布
                    element_types = {}
                    parser_engines = {}
                    for b in blocks:
                        et = b.metadata.get("element_type", "unknown")
                        pe = b.metadata.get("parser_engine", "unknown")
                        element_types[et] = element_types.get(et, 0) + 1
                        parser_engines[pe] = parser_engines.get(pe, 0) + 1
                    
                    log.info(
                        f"[PARSE_OK] doc_id={doc_id} total_blocks={total_blocks} nonempty={nonempty_blocks} "
                        f"total_chars={total_chars} element_types={element_types} parser_engines={parser_engines} "
                        f"elapsed_ms={int((time.perf_counter() - stage_started_at) * 1000)}"
                    )
                except Exception as e:
                    log.error(f"[PARSE_FAIL] doc_id={doc_id} path={doc.local_pdf_path} error={e}")
                    raise
                # 元数据提取阶段
                current_stage = "metadata"
                stage_started_at = time.perf_counter()
                log.info(f"[METADATA_START] doc_id={doc_id}")
                doc = metadata_extractor.extract_and_enrich(db=db, document=doc, blocks=blocks)
                log.info(
                    f"[METADATA_OK] doc_id={doc_id} title={doc.title[:50] if doc.title else 'N/A'} "
                    f"doi={doc.doi or 'N/A'} elapsed_ms={int((time.perf_counter() - stage_started_at) * 1000)}"
                )

                # 结构化阶段
                current_stage = "structure"
                stage_started_at = time.perf_counter()
                log.info(f"[STRUCT_START] doc_id={doc_id}")
                structured_doc = structured_builder.build(document=doc, mineru_blocks=blocks)
                structured_blocks = structured_doc.to_parsed_blocks()
                log.info(
                    f"[STRUCT_OK] doc_id={doc_id} structured_blocks={len(structured_blocks)} "
                    f"logical_types={self._summarize_logical_types(structured_blocks)} "
                    f"elapsed_ms={int((time.perf_counter() - stage_started_at) * 1000)}"
                )
                snapshot = self._build_structure_snapshot(structured_doc)
                try:
                    doc.structure_metadata = snapshot
                    db.add(doc)
                    db.commit()
                    db.refresh(doc)
                except Exception as exc:
                    log.warning(f"[STRUCT_SNAPSHOT_SAVE_FAIL] doc_id={doc_id} err={exc}")

                # 分块阶段 - 详细日志
                current_stage = "chunk"
                try:
                    stage_started_at = time.perf_counter()
                    log.info(f"[CHUNK_START] doc_id={doc_id} input_blocks={len(structured_blocks)}")
                    chunks = chunker.chunk(blocks=structured_blocks)
                    
                    # 为每个 chunk 添加文档级别的元数据（标题、DOI等）
                    chunks_before_post = list(chunks)
                    for c in chunks:
                        if not c.metadata:
                            c.metadata = {}
                        # 添加文档标题（用于引用显示）
                        if doc.title:
                            c.metadata.setdefault("document_title", doc.title)
                        # 添加 DOI（用于引用追溯）
                        if doc.doi:
                            c.metadata.setdefault("doi", doc.doi)
                        # 添加文档名称（fallback：原始文件名 or 标题）
                        if "document_name" not in c.metadata:
                            original_name = None
                            doc_upload_name = getattr(doc, "document_name", None)
                            if doc_upload_name:
                                original_name = doc_upload_name
                            elif doc.local_pdf_path:
                                original_name = os.path.basename(doc.local_pdf_path)
                            elif doc.title:
                                original_name = doc.title
                            if original_name:
                                c.metadata["document_name"] = original_name
                    chunks, chunk_post_stats = post_filter_chunks_for_embedding(
                        chunks,
                        document_title=str(doc.title).strip() if doc.title else None,
                    )
                    if chunk_post_stats:
                        log.info(f"[CHUNK_POST_FILTER] doc_id={doc_id} dropped_stats={chunk_post_stats}")
                    if not chunks:
                        log.warning(
                            f"[CHUNK_POST_FILTER_FALLBACK] doc_id={doc_id} "
                            f"reason=all_chunks_removed_restoring_before_post_filter"
                        )
                        chunks = chunks_before_post

                    # 统计分块结果
                    total_chunks = len(chunks)
                    chunk_element_types = {}
                    for c in chunks:
                        et = c.metadata.get("element_type", "unknown")
                        chunk_element_types[et] = chunk_element_types.get(et, 0) + 1
                    
                    log.info(
                        f"[CHUNK_OK] doc_id={doc_id} output_chunks={total_chunks} "
                        f"element_types={chunk_element_types} "
                        f"elapsed_ms={int((time.perf_counter() - stage_started_at) * 1000)}"
                    )
                except Exception as e:
                    log.error(f"[CHUNK_FAIL] doc_id={doc_id} error={e}")
                    raise
                # 嵌入阶段 - 详细日志
                current_stage = "embed"
                try:
                    stage_started_at = time.perf_counter()
                    log.info(f"[EMBED_START] doc_id={doc_id} input_chunks={len(chunks)}")
                    records = embedder.embed(chunks=chunks)
                    log.info(
                        f"[EMBED_OK] doc_id={doc_id} output_records={len(records)} "
                        f"elapsed_ms={int((time.perf_counter() - stage_started_at) * 1000)}"
                    )
                except Exception as e:
                    log.error(f"[EMBED_FAIL] doc_id={doc_id} error={e}")
                    raise
                
                for idx, rec in enumerate(records):
                    md = rec.setdefault("metadata", {})
                    md.setdefault("kb_id", str(kb_id))
                    md.setdefault("document_id", str(doc_id))
                    md.setdefault("page", md.get("page", 1))
                    md.setdefault("offset_start", md.get("offset_start", 0))
                    md.setdefault("offset_end", md.get("offset_end", 0))
                    if "chunk_id" not in md:
                        md["chunk_id"] = ESIndexer.build_chunk_id(
                            kb_id=kb_id,
                            document_id=doc_id,
                            chunk_index=idx,
                            text=rec.get("text", "") or "",
                            base_id=md.get("id"),
                        )
                    if doc.title:
                        md.setdefault("title", doc.title)
                    if doc.doi:
                        md.setdefault("doi", doc.doi)
                
                # 索引阶段 - 详细日志（包含多模态统计）
                current_stage = "index"
                try:
                    stage_started_at = time.perf_counter()
                    # 统计多模态字段
                    multimodal_stats = {
                        "table_json": 0,
                        "equation_latex": 0,
                        "figure_caption": 0,
                        "has_bbox": 0,
                        "has_confidence": 0,
                    }
                    for rec in records:
                        md = rec.get("metadata", {})
                        if md.get("table_json"):
                            multimodal_stats["table_json"] += 1
                        if md.get("equation_latex"):
                            multimodal_stats["equation_latex"] += 1
                        if md.get("figure_caption"):
                            multimodal_stats["figure_caption"] += 1
                        if md.get("bbox"):
                            multimodal_stats["has_bbox"] += 1
                        if md.get("confidence"):
                            multimodal_stats["has_confidence"] += 1
                    
                    log.info(
                        f"[INDEX_START] doc_id={doc_id} records={len(records)} "
                        f"multimodal={multimodal_stats} index={session_index or 'default'}"
                    )
                    indexer.index(
                        records=records,
                        kb_id=kb_id,
                        document_id=doc_id,
                        session_index=session_index,
                        enable_multimodal_chunks=enable_multimodal,
                    )
                    log.info(
                        f"[INDEX_OK] doc_id={doc_id} "
                        f"elapsed_ms={int((time.perf_counter() - stage_started_at) * 1000)}"
                    )
                except Exception as e:
                    log.error(f"[INDEX_FAIL] doc_id={doc_id} error={e}")
                    raise

                # ScriptLens MVP: knowledge graph 构建已移除
                log.info(
                    f"[DOC_COMPLETE] doc_id={doc_id} chunks={len(records)} "
                    f"elapsed_ms={int((time.perf_counter() - doc_started_at) * 1000)}"
                )
                # Authoritative chunk count comes from the indexer's actual
                # writes, not from in-memory record list (which may include
                # records that the indexer dedup-skipped).
                final_count = count_chunks(db, doc_id)
                mark_ready(db, doc, chunk_count=final_count)
                result.details.append({"doc_id": doc_id, "status": "ok", "chunks": final_count})
                result.succeeded += 1
            except Exception as e:
                # State-machine: any uncaught error in any stage moves the
                # doc to 'failed' with a stage tag. The lifecycle helper also
                # purges any half-written rag_chunks rows so the
                # invariant "ready <=> chunk_count > 0" holds.
                if doc is not None:
                    try:
                        mark_failed(db, doc, stage=current_stage, reason=str(e))
                    except Exception as lifecycle_err:
                        log.warning(
                            f"[LIFECYCLE_WRITE_FAIL] doc_id={doc_id} err={lifecycle_err}"
                        )
                result.details.append({
                    "doc_id": doc_id,
                    "status": "failed",
                    "error": str(e),
                    "stage": current_stage,
                })
                result.failed += 1

        return result

    def _summarize_logical_types(self, blocks: list[ParsedBlock]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for blk in blocks:
            lt = (blk.metadata or {}).get("logical_type", "unknown")
            summary[lt] = summary.get(lt, 0) + 1
        return summary

    def _build_structure_snapshot(self, structured_doc) -> Dict[str, Any]:
        logical_counter: Dict[str, int] = {}
        for blk in structured_doc.blocks:
            logical_counter[blk.logical_type] = logical_counter.get(blk.logical_type, 0) + 1

        max_blocks = getattr(settings, "SM_STRUCTURED_SNAPSHOT_MAX_BLOCKS", 200)
        preview_blocks = []
        for blk in structured_doc.blocks[:max_blocks]:
            meta = blk.metadata or {}
            preview_blocks.append(
                {
                    "block_id": blk.block_id,
                    "logical_type": blk.logical_type,
                    "title": blk.title,
                    "structure_path": blk.structure_path,
                    "level": blk.level,
                    "page_range": meta.get("page_range"),
                    "alignment_status": meta.get("alignment_status"),
                    "source": meta.get("source"),
                    "text_preview": (blk.text or "")[:500],
                }
            )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_blocks": len(structured_doc.blocks),
            "logical_types": logical_counter,
            "blocks": preview_blocks,
        }
