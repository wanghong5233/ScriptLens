from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Dict, Iterable, Optional

from sqlalchemy.exc import SQLAlchemyError

from core.config import settings
from service.core.ingestion.constants import is_multimodal_metadata


class ESIndexer:
    def __init__(self, index_name: str | None = None) -> None:
        self.index_name = index_name or settings.ES_DEFAULT_INDEX
        self.vector_store = str(getattr(settings, "SM_VECTOR_STORE", "pgvector") or "pgvector").strip().lower()
        self.es = None
        if self.vector_store != "pgvector":
            from service.core.rag.utils.es_conn import ESConnection

            self.es = ESConnection()

    @staticmethod
    def build_chunk_id(
        *,
        kb_id: int,
        document_id: int,
        chunk_index: int,
        text: str,
        base_id: Optional[str] = None,
    ) -> str:
        if base_id:
            return base_id
        text_for_id = (text or "")[:2048]
        raw = f"{kb_id}|{document_id}|{chunk_index}|{text_for_id}".encode("utf-8", errors="ignore")
        return hashlib.sha256(raw).hexdigest()

    def index(
        self,
        *,
        records: Iterable[Dict],
        kb_id: int,
        document_id: int,
        session_index: Optional[str] = None,
        enable_multimodal_chunks: Optional[bool] = None,
    ) -> None:
        docs = []
        records_list = list(records)  # 转换为列表以便访问相邻元素
        allow_multimodal = (
            settings.SM_ENABLE_MULTIMODAL_CHUNKS
            if enable_multimodal_chunks is None
            else bool(enable_multimodal_chunks)
        )
        if not allow_multimodal:
            filtered_records = []
            for r in records_list:
                meta = r.get("metadata", {})
                if is_multimodal_metadata(meta):
                    try:
                        logging.getLogger('ragflow.indexer').info(
                            f"[INDEXER_FILTERED] logical_type={meta.get('logical_type')} "
                            f"element_type={meta.get('element_type')} "
                            f"text_preview={r.get('text', '')[:50]}..."
                        )
                    except Exception:
                        pass
                else:
                    filtered_records.append(r)
            records_list = filtered_records
        
        for i, r in enumerate(records_list):
            meta = dict(r.get("metadata", {}))
            logical_type = meta.get("logical_type")
            element_type = meta.get("element_type")
            if not element_type and logical_type:
                meta["element_type"] = logical_type
            elif not element_type:
                meta["element_type"] = meta.get("structure_title") or "unknown"
            meta.setdefault("logical_type", logical_type or meta.get("element_type"))
            meta["kb_id"] = str(kb_id)
            meta["document_id"] = str(document_id)
            meta["chunk_index"] = i  # 记录块在文档中的顺序
            
            # 生成幂等 chunk id（若上游未提供）：sha256(kb_id|doc_id|index|text[:2048])
            base_id = meta.get("id") or meta.get("chunk_id")
            chunk_id = self.build_chunk_id(
                kb_id=kb_id,
                document_id=document_id,
                chunk_index=i,
                text=r.get("text", "") or "",
                base_id=base_id,
            )
            
            # 生成相邻块的 ID
            prev_chunk_id = None
            next_chunk_id = None
            
            if i > 0:
                prev_r = records_list[i - 1]
                prev_meta = prev_r.get("metadata", {}) or {}
                prev_base_id = prev_meta.get("id") or prev_meta.get("chunk_id")
                if prev_base_id:
                    prev_chunk_id = prev_base_id
                else:
                    prev_chunk_id = self.build_chunk_id(
                        kb_id=kb_id,
                        document_id=document_id,
                        chunk_index=i - 1,
                        text=prev_r.get("text", "") or "",
                    )
            
            if i < len(records_list) - 1:
                next_r = records_list[i + 1]
                next_meta = next_r.get("metadata", {}) or {}
                next_base_id = next_meta.get("id") or next_meta.get("chunk_id")
                if next_base_id:
                    next_chunk_id = next_base_id
                else:
                    next_chunk_id = self.build_chunk_id(
                        kb_id=kb_id,
                        document_id=document_id,
                        chunk_index=i + 1,
                        text=next_r.get("text", "") or "",
                    )
            
            # 添加相邻块 ID 到元数据
            if prev_chunk_id:
                meta["prev_chunk_id"] = prev_chunk_id
            if next_chunk_id:
                meta["next_chunk_id"] = next_chunk_id
            
            docs.append({
                "id": chunk_id,
                "text": r.get("text", ""),
                "vector": r.get("vector", []),
                **meta,
            })
        # 交给 ESConnection 批量写入（空列表则跳过）
        if docs:
            target_index = session_index or self.index_name
            if self.vector_store == "pgvector":
                self._write_pgvector_primary(docs=docs, target_index=target_index)
            else:
                # 可观测性：记录写入的索引名
                try:
                    logging.getLogger('ragflow.es_conn').info(
                        f"Indexing {len(docs)} chunks into index '{target_index}' for kb_id={kb_id}, document_id={document_id}"
                    )
                except Exception:
                    pass

                # 分片与指数退避，缓解 429（coordinating bytes 限制）
                batch_size = getattr(settings, "ES_BULK_BATCH_SIZE", 500)
                max_retries = getattr(settings, "ES_BULK_MAX_RETRIES", 4)
                base_sleep = getattr(settings, "ES_BULK_BACKOFF_BASE_SECS", 1.0)

                total = len(docs)
                batches = math.ceil(total / max(1, batch_size))
                es_had_errors = False
                for b in range(batches):
                    start = b * batch_size
                    end = min(total, start + batch_size)
                    slice_docs = docs[start:end]
                    # 重试机制
                    errs = []
                    for attempt in range(max_retries + 1):
                        if self.es is None:
                            raise RuntimeError("ESConnection is not initialized")
                        errs = self.es.insert(slice_docs, target_index)
                        if not errs:
                            break
                        # 仅对 429 or Timeout 退避（es_conn 会把异常转字符串）
                        err_str = "\n".join(errs)
                        if ("429" in err_str) or ("Timeout" in err_str) or ("time out" in err_str):
                            sleep_secs = base_sleep * (2 ** attempt)
                            try:
                                logging.getLogger('ragflow.es_conn').warning(
                                    f"ES bulk retry due to transient error. attempt={attempt} sleep={sleep_secs}s batch={b+1}/{batches} size={len(slice_docs)}"
                                )
                            except Exception:
                                pass
                            time.sleep(sleep_secs)
                            continue
                        # 非可恢复错误，直接跳出重试
                        break
                    if errs:
                        es_had_errors = True

                if es_had_errors:
                    logging.getLogger("ragflow.indexer").warning(
                        "Skip pgvector shadow write because ES indexing reported errors: index=%s",
                        target_index,
                    )
                else:
                    self._shadow_write_pgvector(docs=docs, target_index=target_index)
        else:
            try:
                import logging
                logging.getLogger('ragflow.es_conn').info(f"Indexing skipped: 0 chunks for kb_id={kb_id}, document_id={document_id}, index='{self.index_name}'")
            except Exception:
                pass

        # Hierarchical index: write a document-level summary record
        if docs and getattr(settings, "SM_HIERARCHICAL_INDEX_ENABLED", False):
            self._write_document_summary(
                docs=docs,
                kb_id=kb_id,
                document_id=document_id,
                target_index=session_index or self.index_name,
            )

    def _write_document_summary(
        self,
        *,
        docs: list[Dict],
        kb_id: int,
        document_id: int,
        target_index: str,
    ) -> None:
        """Write a single document-level summary record for hierarchical retrieval."""
        title_parts = []
        abstract_parts = []
        keywords_parts = []

        for d in docs:
            doc_title = d.get("document_title") or d.get("title") or ""
            if doc_title and doc_title not in title_parts:
                title_parts.append(str(doc_title))

            section = str(d.get("section_type") or d.get("section") or "").lower()
            if section in ("abstract",):
                abstract_parts.append(str(d.get("text") or "")[:2000])

            kw = d.get("keywords") or ""
            if kw and str(kw) not in keywords_parts:
                keywords_parts.append(str(kw))

        title_text = " ".join(title_parts)[:500]
        abstract_text = " ".join(abstract_parts)[:3000]
        keywords_text = " ".join(keywords_parts)[:500]

        summary_text = f"{title_text}\n{abstract_text}\n{keywords_text}".strip()
        if not summary_text or len(summary_text) < 20:
            return

        summary_id = hashlib.sha256(
            f"doc_summary|{kb_id}|{document_id}".encode("utf-8")
        ).hexdigest()

        try:
            from service.core.ingestion.embedder import SimpleAPIEmbedder
            embedder = SimpleAPIEmbedder()
            vectors = embedder.embed([summary_text])
            vector = vectors[0] if vectors else []
        except Exception:
            vector = []

        summary_doc = {
            "id": summary_id,
            "text": summary_text,
            "vector": vector,
            "kb_id": str(kb_id),
            "document_id": str(document_id),
            "level": "document",
            "element_type": "document_summary",
            "chunk_index": -1,
        }

        if self.vector_store == "pgvector":
            try:
                from service.core.ingestion.pgvector_writer import PgVectorChunkWriter

                PgVectorChunkWriter().upsert_chunks(
                    records=[summary_doc],
                    index_name=target_index,
                )
                logging.getLogger("ragflow.indexer").info(
                    "Indexed pgvector document-level summary for doc_id=%s (%d chars)",
                    document_id,
                    len(summary_text),
                )
            except (SQLAlchemyError, ValueError, TypeError) as exc:
                logging.getLogger("ragflow.indexer").warning(
                    "pgvector document summary indexing error for doc_id=%s: %s",
                    document_id,
                    exc,
                )
            return

        try:
            if self.es is None:
                raise RuntimeError("ESConnection is not initialized")
            errs = self.es.insert([summary_doc], target_index)
            if errs:
                logging.getLogger("ragflow.indexer").warning(
                    "Failed to index document summary for doc_id=%s: %s",
                    document_id, errs[:2],
                )
            else:
                logging.getLogger("ragflow.indexer").info(
                    "Indexed document-level summary for doc_id=%s (%d chars)",
                    document_id, len(summary_text),
                )
        except Exception as exc:
            logging.getLogger("ragflow.indexer").warning(
                "Document summary indexing error for doc_id=%s: %s",
                document_id, exc,
            )

    def _shadow_write_pgvector(self, *, docs: list[Dict], target_index: str) -> None:
        """Optionally mirror ES chunks into pgvector without changing the primary path."""

        if not getattr(settings, "SM_PGVECTOR_DUAL_WRITE_ENABLED", False):
            return

        try:
            from service.core.ingestion.pgvector_writer import PgVectorChunkWriter

            PgVectorChunkWriter().upsert_chunks(
                records=docs,
                index_name=target_index,
            )
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            logging.getLogger("ragflow.indexer").warning(
                "pgvector shadow write failed for index=%s: %s",
                target_index,
                exc,
            )
            if getattr(settings, "SM_PGVECTOR_DUAL_WRITE_STRICT", False):
                raise

    def _write_pgvector_primary(self, *, docs: list[Dict], target_index: str) -> None:
        """Write chunks to pgvector as the primary vector store."""

        try:
            from service.core.ingestion.pgvector_writer import PgVectorChunkWriter

            written = PgVectorChunkWriter().upsert_chunks(
                records=docs,
                index_name=target_index,
            )
            logging.getLogger("ragflow.indexer").info(
                "Indexed %s chunks into pgvector table '%s' for index '%s'",
                written,
                settings.SM_PGVECTOR_TABLE,
                target_index,
            )
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            logging.getLogger("ragflow.indexer").exception(
                "pgvector primary write failed for index=%s: %s",
                target_index,
                exc,
            )
            raise
