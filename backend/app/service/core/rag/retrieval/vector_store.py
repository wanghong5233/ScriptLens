from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import logging
import time

from sqlalchemy import text

from core.config import settings
from service.core.rag.nlp.model import generate_embedding
from utils.database import engine

try:
    from elasticsearch import NotFoundError
except ImportError:
    class NotFoundError(RuntimeError):
        """Placeholder used only when the legacy ES dependency is absent."""

        pass


@dataclass
class RetrieveQuery:
    text: str
    kb_id: int
    top_k: int = 5
    focus_doc_ids: Optional[List[int]] = None
    index_override: Optional[str] = None  # for session-level index
    use_vector: bool = True  # enable hybrid retrieval (text + vector)
    channels: Optional[List[str]] = None  # explicitly requested recall sources
    query_tag: Optional[str] = None  # e.g. original / sub-query / hyde
    synthetic: bool = False  # whether the query is HyDE-generated
    embedding_override: Optional[List[float]] = None  # pre-computed embedding (HyDE)
    boost_doc_ids: Optional[List[int]] = None  # 记忆引导增强的 doc_id 列表
    fallback_index: Optional[str] = None  # 当指定索引不存在时的回退索引
    channel_topk_cap: Optional[int] = None  # 每路召回上限（用于快速模式硬限流）


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float
    metadata: Dict[str, Any]
    source: str = "hybrid"
    query: Optional[str] = None
    rank: Optional[int] = None


class VectorStore:
    def search(self, *, query: RetrieveQuery) -> List[RetrievedChunk]:
        raise NotImplementedError


class PgVectorStore(VectorStore):
    """PostgreSQL + pgvector retrieval implementation for staged ES migration."""

    def __init__(self, default_index: str | None = None, table_name: str | None = None) -> None:
        self.default_index = default_index or settings.ES_DEFAULT_INDEX
        self.table_name = self._validate_table_name(table_name or settings.SM_PGVECTOR_TABLE)
        self.logger = logging.getLogger("rag.retriever.pgvector")

    def search(self, *, query: RetrieveQuery) -> List[RetrievedChunk]:
        channels = query.channels or (["bm25", "vector"] if query.use_vector else ["bm25"])
        candidates = self.search_multi_path(query=query, channels=channels)
        return self._fuse_for_basic(candidates=candidates, top_k=query.top_k)

    def search_multi_path(
        self,
        *,
        query: RetrieveQuery,
        channels: Optional[List[str]] = None,
        candidate_multiplier: Optional[int] = None,
    ) -> List[RetrievedChunk]:
        resolved_channels = self._resolve_channels(channels or query.channels)
        if not resolved_channels:
            return []

        multiplier = candidate_multiplier or max(int(getattr(settings, "SM_RECALL_CANDIDATE_MULTIPLIER", 3) or 3), 1)
        per_channel_limit = {
            "bm25": max(int(getattr(settings, "SM_BM25_TOPK", 0) or 0), query.top_k * multiplier),
            "vector": max(int(getattr(settings, "SM_VECTOR_TOPK", 0) or 0), query.top_k * multiplier),
        }
        aggregated: List[RetrievedChunk] = []
        index_name = self._resolve_index(query)
        for channel in resolved_channels:
            ch = channel.lower().strip()
            limit = per_channel_limit.get(ch, query.top_k * multiplier)
            limit = max(limit, query.top_k)
            if isinstance(query.channel_topk_cap, int) and query.channel_topk_cap > 0:
                limit = max(query.top_k, min(limit, query.channel_topk_cap))

            if ch == "bm25":
                hits = self._search_bm25(query=query, index_name=index_name, limit=limit)
            elif ch == "vector":
                hits = self._search_vector(query=query, index_name=index_name, limit=limit)
            elif ch == "colbert":
                hits = []
            else:
                continue

            for rank, hit in enumerate(hits, start=1):
                hit.source = ch
                hit.query = query.text
                hit.rank = rank
                if query.query_tag:
                    hit.metadata["query_tag"] = query.query_tag
                hit.metadata["query_text"] = query.text
                hit.metadata["query_synthetic"] = bool(query.synthetic)
                aggregated.append(hit)

        return self._filter_short_chunks(aggregated)

    def index_exists(self, index_name: str) -> bool:
        if not index_name:
            return False
        statement = text(
            f"SELECT 1 FROM {self.table_name} WHERE index_name = :index_name LIMIT 1"
        )
        with engine.connect() as connection:
            row = connection.execute(statement, {"index_name": index_name}).first()
        return row is not None

    def _resolve_index(self, query: RetrieveQuery) -> str:
        return query.index_override or self.default_index or "scholarmind_default"

    def _resolve_channels(self, override: Optional[List[str]]) -> List[str]:
        if override:
            return [c.strip() for c in override if c and c.strip()]
        raw = getattr(settings, "SM_RECALL_SOURCES", "bm25,vector")
        return [c.strip() for c in raw.split(",") if c.strip()]

    def _search_bm25(self, *, query: RetrieveQuery, index_name: str, limit: int) -> List[RetrievedChunk]:
        filters, params = self._filters(query=query, index_name=index_name)
        params.update({"query_text": query.text, "limit": limit})
        statement = text(
            f"""
            SELECT
                chunk_id,
                text,
                metadata,
                index_name,
                ts_rank_cd(to_tsvector('simple', coalesce(text, '')), plainto_tsquery('simple', :query_text)) AS score
            FROM {self.table_name}
            WHERE {' AND '.join(filters)}
              AND plainto_tsquery('simple', :query_text) @@ to_tsvector('simple', coalesce(text, ''))
            ORDER BY score DESC, chunk_index ASC
            LIMIT :limit
            """
        )
        rows = self._execute_rows(statement, params)
        return self._rows_to_chunks(rows=rows, source="bm25", boost_doc_ids=query.boost_doc_ids)

    def _search_vector(self, *, query: RetrieveQuery, index_name: str, limit: int) -> List[RetrievedChunk]:
        embedding = self._get_embedding(query)
        if embedding is None:
            return []
        filters, params = self._filters(query=query, index_name=index_name)
        params.update({"embedding": self._vector_literal(embedding), "limit": limit})
        statement = text(
            f"""
            SELECT
                chunk_id,
                text,
                metadata,
                index_name,
                (1.0 - (embedding <=> CAST(:embedding AS vector))) AS score
            FROM {self.table_name}
            WHERE {' AND '.join(filters)}
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS vector), chunk_index ASC
            LIMIT :limit
            """
        )
        rows = self._execute_rows(statement, params)
        return self._rows_to_chunks(rows=rows, source="vector", boost_doc_ids=query.boost_doc_ids)

    def _filters(self, *, query: RetrieveQuery, index_name: str) -> tuple[List[str], Dict[str, Any]]:
        filters = ["index_name = :index_name", "kb_id = :kb_id"]
        params: Dict[str, Any] = {"index_name": index_name, "kb_id": int(query.kb_id)}
        if query.focus_doc_ids:
            doc_ids = [int(doc_id) for doc_id in query.focus_doc_ids if doc_id is not None]
            if doc_ids:
                placeholders = []
                for idx, doc_id in enumerate(doc_ids):
                    name = f"doc_id_{idx}"
                    placeholders.append(f":{name}")
                    params[name] = doc_id
                filters.append(f"document_id IN ({', '.join(placeholders)})")
        return filters, params

    def _execute_rows(self, statement: Any, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        with engine.connect() as connection:
            result = connection.execute(statement, params)
            return [dict(row._mapping) for row in result]

    def _rows_to_chunks(
        self,
        *,
        rows: List[Dict[str, Any]],
        source: str,
        boost_doc_ids: Optional[List[int]],
    ) -> List[RetrievedChunk]:
        boost_ids = {str(doc) for doc in (boost_doc_ids or []) if doc is not None}
        mem_boost = float(getattr(settings, "SM_MEMORY_DOC_BOOST", 0.3) or 0.3)
        chunks: List[RetrievedChunk] = []
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            metadata["index_name"] = row.get("index_name")
            metadata["retrieval_source"] = source
            doc_id = metadata.get("document_id")
            score = float(row.get("score") or 0.0)
            if doc_id is not None and str(doc_id) in boost_ids:
                score *= 1.0 + mem_boost
                metadata["memory_boost"] = True
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(row.get("chunk_id") or ""),
                    text=str(row.get("text") or ""),
                    score=score,
                    metadata=metadata,
                    source=source,
                )
            )
        return chunks

    def _get_embedding(self, query: RetrieveQuery) -> Optional[List[float]]:
        if query.embedding_override is not None:
            return query.embedding_override
        try:
            vecs = generate_embedding([query.text])
            if vecs and vecs[0] is not None:
                return vecs[0]
        except Exception as exc:
            self.logger.warning("PgVectorRetriever embedding generation failed: %s", exc)
        return None

    def _vector_literal(self, embedding: List[float]) -> str:
        return "[" + ",".join(str(float(item)) for item in embedding) + "]"

    def _fetch_chunks_by_ids(
        self,
        *,
        chunk_ids: List[str],
        index_name: str,
        kb_id: int,
    ) -> List[RetrievedChunk]:
        if not chunk_ids:
            return []
        placeholders = []
        params: Dict[str, Any] = {"index_name": index_name, "kb_id": int(kb_id)}
        for idx, chunk_id in enumerate(chunk_ids):
            name = f"chunk_id_{idx}"
            placeholders.append(f":{name}")
            params[name] = chunk_id
        statement = text(
            f"""
            SELECT chunk_id, text, metadata, index_name, 0.0 AS score
            FROM {self.table_name}
            WHERE index_name = :index_name
              AND kb_id = :kb_id
              AND chunk_id IN ({', '.join(placeholders)})
            """
        )
        rows = self._execute_rows(statement, params)
        return self._rows_to_chunks(rows=rows, source="context", boost_doc_ids=None)

    def _expand_equation_context(
        self,
        chunks: List[RetrievedChunk],
        index_name: Optional[str],
        kb_id: int,
    ) -> List[RetrievedChunk]:
        equation_chunks = [
            chunk
            for chunk in chunks
            if str((chunk.metadata or {}).get("element_type") or "").lower() == "equation_latex"
        ]
        if not equation_chunks:
            return chunks
        expanded_equations = self._expand_context_by_neighbor_ids(
            chunks=equation_chunks,
            index_name=index_name,
            kb_id=kb_id,
        )
        seen = {chunk.chunk_id for chunk in chunks}
        result = list(chunks)
        for chunk in expanded_equations:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            result.append(chunk)
        return result

    def _expand_context_window(
        self,
        chunks: List[RetrievedChunk],
        index_name: Optional[str],
        kb_id: int,
    ) -> List[RetrievedChunk]:
        if not getattr(settings, "SM_CONTEXT_WINDOW_EXPANSION_ENABLED", False):
            return chunks
        return self._expand_context_by_neighbor_ids(chunks=chunks, index_name=index_name, kb_id=kb_id)

    def _expand_context_by_neighbor_ids(
        self,
        *,
        chunks: List[RetrievedChunk],
        index_name: Optional[str],
        kb_id: int,
    ) -> List[RetrievedChunk]:
        expanded = list(chunks)
        seen = {chunk.chunk_id for chunk in expanded}
        for chunk in chunks:
            effective_index = (chunk.metadata or {}).get("index_name") or index_name
            if not effective_index:
                continue
            neighbor_ids = [
                value
                for value in [
                    (chunk.metadata or {}).get("prev_chunk_id"),
                    (chunk.metadata or {}).get("next_chunk_id"),
                ]
                if value and value not in seen
            ]
            context_chunks = self._fetch_chunks_by_ids(
                chunk_ids=[str(item) for item in neighbor_ids],
                index_name=str(effective_index),
                kb_id=kb_id,
            )
            for context_chunk in context_chunks:
                if context_chunk.chunk_id in seen:
                    continue
                seen.add(context_chunk.chunk_id)
                context_chunk.score = float(chunk.score or 0.0) * 0.4
                context_chunk.metadata["is_context"] = True
                context_chunk.metadata["context_for_chunk_id"] = chunk.chunk_id
                expanded.append(context_chunk)
        return expanded

    def _filter_short_chunks(self, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        min_text_chars = max(
            int(getattr(settings, "SM_RETRIEVAL_MIN_TEXT_CHARS", 0) or getattr(settings, "SM_CHUNK_MIN_FILTER_CHARS", 0) or 0),
            0,
        )
        if min_text_chars <= 0:
            return chunks
        allowed_multimodal = {"equation_latex", "table_json", "figure_summary"}
        filtered = []
        for chunk in chunks:
            md = chunk.metadata or {}
            element_type = str(md.get("element_type") or "").lower()
            logical_type = str(md.get("logical_type") or "").lower()
            if element_type in allowed_multimodal or logical_type in allowed_multimodal:
                filtered.append(chunk)
                continue
            if len((chunk.text or "").strip()) >= min_text_chars:
                filtered.append(chunk)
        return filtered

    def _fuse_for_basic(self, *, candidates: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
        best_by_chunk: Dict[str, RetrievedChunk] = {}
        for candidate in candidates:
            existing = best_by_chunk.get(candidate.chunk_id)
            if existing is None or candidate.score > existing.score:
                best_by_chunk[candidate.chunk_id] = candidate
        return sorted(best_by_chunk.values(), key=lambda item: float(item.score or 0.0), reverse=True)[:top_k]

    def _validate_table_name(self, table_name: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name or ""):
            raise ValueError(f"Invalid pgvector table name: {table_name!r}")
        return table_name


class ESVectoreStore(VectorStore):
    def __init__(self, default_index: str | None = None) -> None:
        from service.core.rag.utils.es_conn import ESConnection

        self.es = ESConnection()
        self.default_index = default_index
        self.logger = logging.getLogger("rag.retriever.es")
        self._bm25_fields_cache: Optional[List[str]] = None
        self._index_exists_cache: Dict[str, tuple[bool, float]] = {}

    # ---------------------------------------------------------------------
    # Public APIs
    # ---------------------------------------------------------------------
    def search(self, *, query: RetrieveQuery) -> List[RetrievedChunk]:
        """Legacy search entry kept for backward compatibility."""
        index_name = self._resolve_index(query)
        channels = query.channels or (["bm25", "vector"] if query.use_vector else ["bm25"])
        t0 = time.time()
        candidates = self.search_multi_path(query=query, channels=channels)
        fused = self._fuse_for_basic(candidates=candidates, top_k=query.top_k)
        if getattr(settings, "SM_EQUATION_CONTEXT_EXPANSION", True):
            fused = self._expand_equation_context(
                chunks=fused,
                index_name=index_name,
                kb_id=query.kb_id,
            )
        took_ms = int((time.time() - t0) * 1000)
        try:
            self.logger.info(
                "ESRetriever[legacy] q='%s' kb=%s channels=%s candidates=%s final=%s took_ms=%s",
                (query.text or "")[:64],
                query.kb_id,
                ",".join(channels),
                len(candidates),
                len(fused),
                took_ms,
            )
        except Exception:
            pass
        return fused

    def search_multi_path(
        self,
        *,
        query: RetrieveQuery,
        channels: Optional[List[str]] = None,
        candidate_multiplier: Optional[int] = None,
    ) -> List[RetrievedChunk]:
        """Perform multi-path recall (BM25 / vector / ColBERT)."""
        index_name = self._resolve_index(query)
        resolved_channels = self._resolve_channels(channels or query.channels)
        if not resolved_channels:
            return []

        multiplier = candidate_multiplier or max(int(getattr(settings, "SM_RECALL_CANDIDATE_MULTIPLIER", 3) or 3), 1)
        per_channel_limit = {
            "bm25": max(int(getattr(settings, "SM_BM25_TOPK", 0) or 0), query.top_k * multiplier),
            "vector": max(int(getattr(settings, "SM_VECTOR_TOPK", 0) or 0), query.top_k * multiplier),
            "colbert": max(int(getattr(settings, "SM_COLBERT_TOPK", 0) or 0), query.top_k * multiplier),
        }

        aggregated: List[RetrievedChunk] = []
        for channel in resolved_channels:
            ch = channel.lower().strip()
            limit = per_channel_limit.get(ch, query.top_k * multiplier)
            limit = max(limit, query.top_k)
            if isinstance(query.channel_topk_cap, int) and query.channel_topk_cap > 0:
                limit = min(limit, query.channel_topk_cap)
                limit = max(limit, query.top_k)
            hits: List[RetrievedChunk] = []

            if ch == "bm25":
                hits = self._search_bm25(query, index_name, limit)
            elif ch == "vector":
                hits = self._search_vector(query, index_name, limit)
            elif ch == "colbert":
                hits = self._search_colbert(query, limit)
            else:
                continue

            for rank, hit in enumerate(hits, start=1):
                hit.source = ch
                hit.query = query.text
                hit.rank = rank
                if query.query_tag:
                    hit.metadata["query_tag"] = query.query_tag
                hit.metadata["query_text"] = query.text
                hit.metadata["query_synthetic"] = bool(query.synthetic)
                aggregated.append(hit)

            try:
                self.logger.debug(
                    "ESRetriever[channel=%s] q='%s' candidates=%s limit=%s",
                    ch,
                    (query.text or "")[:64],
                    len(hits),
                    limit,
                )
            except Exception:
                pass

        min_text_chars = max(int(getattr(settings, "SM_RETRIEVAL_MIN_TEXT_CHARS", 0) or getattr(settings, "SM_CHUNK_MIN_FILTER_CHARS", 0) or 0), 0)
        if min_text_chars > 0 and aggregated:
            before = len(aggregated)
            allowed_multimodal = {"equation_latex", "table_json", "figure_summary"}

            def _is_short_and_not_multimodal(hit: RetrievedChunk) -> bool:
                text_len = len((hit.text or "").strip())
                md = hit.metadata or {}
                element_type = str(md.get("element_type") or "").lower()
                logical_type = str(md.get("logical_type") or "").lower()
                if element_type in allowed_multimodal or logical_type in allowed_multimodal:
                    return False
                return text_len < min_text_chars

            aggregated = [hit for hit in aggregated if not _is_short_and_not_multimodal(hit)]
            removed = before - len(aggregated)
            if removed > 0:
                try:
                    self.logger.debug(
                        "ESRetriever filtered %s short candidates (<%s chars) for query '%s'",
                        removed,
                        min_text_chars,
                        (query.text or "")[:64],
                    )
                except Exception:
                    pass

        return aggregated

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _resolve_index(self, query: RetrieveQuery) -> str:
        return query.index_override or self.default_index or "scholarmind_default"

    def index_exists(self, index_name: str) -> bool:
        if not index_name:
            return False
        ttl = float(getattr(settings, "SM_INDEX_EXISTS_CACHE_TTL", 60) or 60)
        now = time.time()
        cached = self._index_exists_cache.get(index_name)
        if cached and (now - cached[1]) <= ttl:
            return cached[0]
        exists = bool(self.es.index_exists(index_name))
        self._index_exists_cache[index_name] = (exists, now)
        return exists

    def _resolve_channels(self, override: Optional[List[str]]) -> List[str]:
        if override:
            return [c.strip() for c in override if c and c.strip()]
        raw = getattr(settings, "SM_RECALL_SOURCES", "bm25,vector")
        return [c.strip() for c in raw.split(",") if c.strip()]

    def _select_fields(self) -> List[str]:
        return [
            "text",
            "kb_id",
            "document_id",
            "page",
            "offset_start",
            "offset_end",
            "element_type",
            "prev_chunk_id",
            "next_chunk_id",
            "chunk_index",
            "section",
            "section_type",
            "publication_year",
            "citation_count",
            # 结构化元数据（用于前端精确定位和引用显示）
            "logical_type",
            "structure_path",
            "structure_title",
            "structure_chunk_index",
            "structure_chunk_total",
            "page_range",
            "bbox_list",
            "alignment_status",
            "source",
            "parser_engine",
            "document_title",
            "document_name",
            "doi",
        ]

    def _parse_bm25_fields(self) -> List[str]:
        if self._bm25_fields_cache is None:
            raw = getattr(settings, "SM_BM25_FIELDS", "text")
            fields = [seg.strip() for seg in raw.split(",") if seg.strip()]
            self._bm25_fields_cache = fields or ["text"]
        return self._bm25_fields_cache

    def _build_condition(self, query: RetrieveQuery) -> Dict[str, Any]:
        condition: Dict[str, Any] = {}
        if query.focus_doc_ids:
            condition["document_id"] = [str(d) for d in query.focus_doc_ids if d is not None]
        return condition

    def _transform_hits(
        self,
        res: Dict[str, Any],
        *,
        source: str,
        score_boost: float = 1.0,
        boost_doc_ids: Optional[set[str]] = None,
        index_name: Optional[str] = None,
    ) -> List[RetrievedChunk]:
        hits = res.get("hits", {}).get("hits", [])
        transformed: List[RetrievedChunk] = []
        mem_boost = float(getattr(settings, "SM_MEMORY_DOC_BOOST", 0.3) or 0.3)
        for h in hits:
            src = h.get("_source", {})
            doc_id = src.get("document_id")
            doc_id_str = str(doc_id) if doc_id is not None else None
            boost_ratio = 1.0
            boosted_flag = False
            if boost_doc_ids and doc_id_str and doc_id_str in boost_doc_ids:
                boost_ratio += mem_boost
                boosted_flag = True
            metadata = {
                "kb_id": src.get("kb_id"),
                "document_id": src.get("document_id"),
                "page": src.get("page"),
                "offset_start": src.get("offset_start"),
                "offset_end": src.get("offset_end"),
                "element_type": src.get("element_type"),
                "prev_chunk_id": src.get("prev_chunk_id"),
                "next_chunk_id": src.get("next_chunk_id"),
                "chunk_index": src.get("chunk_index"),
                "section": src.get("section") or src.get("section_type"),
                "section_type": src.get("section_type"),
                "publication_year": src.get("publication_year"),
                "citation_count": src.get("citation_count"),
                "retrieval_source": source,
                # 结构化元数据（用于前端精确定位和引用显示）
                "logical_type": src.get("logical_type"),
                "structure_path": src.get("structure_path"),
                "structure_title": src.get("structure_title"),
                "structure_chunk_index": src.get("structure_chunk_index"),
                "structure_chunk_total": src.get("structure_chunk_total"),
                "page_range": src.get("page_range"),
                "bbox_list": src.get("bbox_list"),
                "alignment_status": src.get("alignment_status"),
                "source": src.get("source"),
                "parser_engine": src.get("parser_engine"),
                "document_title": src.get("document_title"),
                "document_name": src.get("document_name"),
                "doi": src.get("doi"),
            }
            if index_name:
                metadata["index_name"] = index_name
            if boosted_flag:
                metadata["memory_boost"] = True
            transformed.append(
                RetrievedChunk(
                    chunk_id=h.get("_id", ""),
                    text=src.get("text", ""),
                    score=float(h.get("_score", 0.0) or 0.0) * score_boost * boost_ratio,
                    metadata=metadata,
                    source=source,
                )
            )
        return transformed

    def _order_key(self, chunk: RetrievedChunk) -> tuple[float, int, int]:
        md = chunk.metadata or {}
        page = md.get("page") or 1
        offset = md.get("offset_start") or 0
        try:
            page = int(page)
        except Exception:
            page = 1
        try:
            offset = int(offset)
        except Exception:
            offset = 0
        return -float(chunk.score or 0.0), page, offset

    def _chunk_location_key(self, chunk: RetrievedChunk) -> str:
        md = chunk.metadata or {}
        doc_id = str(md.get("document_id") or "")
        page = str(md.get("page") or "")
        off_s = md.get("offset_start")
        off_e = md.get("offset_end")
        if off_s is None or off_e is None or str(off_s) == "" or str(off_e) == "":
            prefix = (chunk.text or "")[:64].strip().lower()
            return f"{doc_id}:{page}:{prefix}"
        return f"{doc_id}-{page}-{off_s}-{off_e}"

    def _deduplicate_by_location(self, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        seen_keys: set[str] = set()
        deduped: List[RetrievedChunk] = []
        for chunk in chunks:
            key = self._chunk_location_key(chunk)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(chunk)
        return deduped

    def _get_embedding(self, query: RetrieveQuery) -> Optional[List[float]]:
        if query.embedding_override is not None:
            return query.embedding_override
        try:
            vecs = generate_embedding([query.text])
            if vecs and vecs[0] is not None:
                return vecs[0]
        except Exception as exc:
            try:
                self.logger.warning("ESRetriever embedding generation failed: %s", exc)
            except Exception:
                pass
        return None

    def _search_bm25(self, query: RetrieveQuery, index_name: str, limit: int) -> List[RetrievedChunk]:
        from service.core.rag.utils.doc_store_conn import MatchTextExpr, OrderByExpr

        match_expr = MatchTextExpr(
            fields=self._parse_bm25_fields(),
            matching_text=query.text,
            topn=limit,
            extra_options={"minimum_should_match": 0.0},
        )
        boost_ids = {str(doc) for doc in (query.boost_doc_ids or []) if doc is not None}

        def _execute(target_index: str):
            return self.es.search(
                selectFields=self._select_fields(),
                highlightFields=[],
                condition=self._build_condition(query),
                matchExprs=[match_expr],
                orderBy=OrderByExpr().desc("_score"),
                offset=0,
                limit=limit,
                indexNames=target_index,
                knowledgebaseIds=[str(query.kb_id)],
                aggFields=[],
                rank_feature=None,
            )

        active_index = index_name
        fallback_index = query.fallback_index
        if fallback_index and fallback_index == index_name:
            fallback_index = None

        try:
            res = _execute(index_name)
        except NotFoundError:
            if fallback_index and fallback_index != index_name:
                try:
                    self.logger.warning(
                        "ES index '%s' not found, fallback to '%s' for BM25 recall.",
                        index_name,
                        fallback_index,
                    )
                except Exception:
                    pass
                active_index = fallback_index
                res = _execute(fallback_index)
            else:
                raise
        return self._transform_hits(res, source="bm25", boost_doc_ids=boost_ids, index_name=active_index)

    def _search_vector(self, query: RetrieveQuery, index_name: str, limit: int) -> List[RetrievedChunk]:
        from service.core.rag.utils.doc_store_conn import MatchDenseExpr, OrderByExpr

        embedding = self._get_embedding(query)
        if embedding is None:
            return []

        match_expr = MatchDenseExpr(
            vector_column_name="vector",
            embedding_data=embedding,
            embedding_data_type="float32",
            distance_type="cosine",
            topn=limit,
            extra_options={"similarity": 0.0},
        )
        boost_ids = {str(doc) for doc in (query.boost_doc_ids or []) if doc is not None}

        def _execute(target_index: str):
            return self.es.search(
                selectFields=self._select_fields(),
                highlightFields=[],
                condition=self._build_condition(query),
                matchExprs=[match_expr],
                orderBy=OrderByExpr().desc("_score"),
                offset=0,
                limit=limit,
                indexNames=target_index,
                knowledgebaseIds=[str(query.kb_id)],
                aggFields=[],
                rank_feature=None,
            )

        active_index = index_name
        fallback_index = query.fallback_index
        if fallback_index and fallback_index == index_name:
            fallback_index = None

        try:
            res = _execute(index_name)
        except NotFoundError:
            if fallback_index and fallback_index != index_name:
                try:
                    self.logger.warning(
                        "ES index '%s' not found, fallback to '%s' for vector recall.",
                        index_name,
                        fallback_index,
                    )
                except Exception:
                    pass
                active_index = fallback_index
                res = _execute(fallback_index)
            else:
                raise
        return self._transform_hits(res, source="vector", boost_doc_ids=boost_ids, index_name=active_index)

    def _search_colbert(self, query: RetrieveQuery, limit: int) -> List[RetrievedChunk]:
        if not getattr(settings, "SM_COLBERT_ENABLED", False):
            return []
        endpoint = getattr(settings, "SM_COLBERT_ENDPOINT", None)
        if not endpoint:
            try:
                self.logger.debug("ColBERT channel requested but endpoint is not configured.")
            except Exception:
                pass
            return []
        try:
            import requests  # type: ignore[import]
        except ImportError:
            try:
                self.logger.warning("requests not installed, skip ColBERT recall path.")
            except Exception:
                pass
            return []

        payload = {
            "query": query.text,
            "kb_id": query.kb_id,
            "top_k": limit,
            "focus_doc_ids": [int(d) for d in (query.focus_doc_ids or [])],
        }
        try:
            response = requests.post(endpoint, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            try:
                self.logger.warning("ColBERT recall failed: %s", exc)
            except Exception:
                pass
            return []

        results = data.get("results", []) if isinstance(data, dict) else []
        transformed: List[RetrievedChunk] = []
        boost_ids = {str(doc) for doc in (query.boost_doc_ids or []) if doc is not None}
        for item in results[:limit]:
            metadata = item.get("metadata") or {}
            doc_id = metadata.get("document_id")
            doc_id_str = str(doc_id) if doc_id is not None else None
            boosted_flag = bool(boost_ids and doc_id_str in boost_ids)
            if boosted_flag:
                metadata["memory_boost"] = True
            transformed.append(
                RetrievedChunk(
                    chunk_id=item.get("chunk_id") or metadata.get("chunk_id") or "",
                    text=item.get("text") or "",
                    score=float(item.get("score", 0.0) or 0.0) * (1.0 + (float(getattr(settings, "SM_MEMORY_DOC_BOOST", 0.3) or 0.3) if boosted_flag else 0.0)),
                    metadata=metadata,
                    source="colbert",
                )
            )
        return transformed

    def _fuse_for_basic(self, *, candidates: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
        if not candidates:
            return []

        best_by_chunk: Dict[str, RetrievedChunk] = {}
        for cand in candidates:
            existing = best_by_chunk.get(cand.chunk_id)
            if existing is None or cand.score > existing.score:
                best_by_chunk[cand.chunk_id] = cand

        ordered = sorted(best_by_chunk.values(), key=self._order_key)
        deduped = self._deduplicate_by_location(ordered)
        return deduped[:top_k]

    # ---------------------------------------------------------------------
    # Equation context expansion
    # ---------------------------------------------------------------------
    def _expand_equation_context(
        self,
        chunks: List[RetrievedChunk],
        index_name: Optional[str],
        kb_id: int,
    ) -> List[RetrievedChunk]:
        from core.config import settings as _settings

        prev_count = int(getattr(_settings, "SM_EQUATION_EXPANSION_PREV", 1) or 1)
        next_count = int(getattr(_settings, "SM_EQUATION_EXPANSION_NEXT", 1) or 1)

        expanded_chunks: List[RetrievedChunk] = []
        seen_chunk_ids: set[str] = set()

        for chunk in chunks:
            expanded_chunks.append(chunk)
            seen_chunk_ids.add(chunk.chunk_id)

            element_type = (chunk.metadata or {}).get("element_type", "")
            if element_type != "equation_latex":
                continue

            prev_id = chunk.metadata.get("prev_chunk_id")
            next_id = chunk.metadata.get("next_chunk_id")

            context_ids: List[str] = []
            if prev_id and prev_count > 0:
                context_ids.append(prev_id)
            if next_id and next_count > 0:
                context_ids.append(next_id)

            if not context_ids:
                continue

            effective_index = (chunk.metadata or {}).get("index_name") or index_name
            if not effective_index:
                continue

            try:
                context_chunks = self._fetch_chunks_by_ids(
                    chunk_ids=context_ids,
                    index_name=effective_index,
                    kb_id=kb_id,
                )
                for ctx_chunk in context_chunks:
                    if ctx_chunk.chunk_id in seen_chunk_ids:
                        continue
                    seen_chunk_ids.add(ctx_chunk.chunk_id)
                    ctx_chunk.score = chunk.score * 0.5
                    ctx_chunk.metadata["is_context"] = True
                    ctx_chunk.metadata["context_for_chunk_id"] = chunk.chunk_id
                    if effective_index:
                        ctx_chunk.metadata.setdefault("index_name", effective_index)
                    expanded_chunks.append(ctx_chunk)

                try:
                    self.logger.debug(
                        "EquationContext added %s context chunks for %s",
                        len(context_chunks),
                        chunk.chunk_id,
                    )
                except Exception:
                    pass
            except Exception as exc:
                try:
                    self.logger.warning("Failed to expand context for %s: %s", chunk.chunk_id, exc)
                except Exception:
                    pass

        return expanded_chunks

    # ---------------------------------------------------------------------
    # Generic context window expansion (all chunk types)
    # ---------------------------------------------------------------------
    def _expand_context_window(
        self,
        chunks: List[RetrievedChunk],
        index_name: Optional[str],
        kb_id: int,
    ) -> List[RetrievedChunk]:
        from core.config import settings as _settings

        if not getattr(_settings, "SM_CONTEXT_WINDOW_EXPANSION_ENABLED", False):
            return chunks

        prev_count = int(getattr(_settings, "SM_CONTEXT_WINDOW_EXPANSION_PREV", 1) or 1)
        next_count = int(getattr(_settings, "SM_CONTEXT_WINDOW_EXPANSION_NEXT", 1) or 1)
        if prev_count <= 0 and next_count <= 0:
            return chunks

        expanded_chunks: List[RetrievedChunk] = []
        seen_chunk_ids: set[str] = set()

        for chunk in chunks:
            expanded_chunks.append(chunk)
            seen_chunk_ids.add(chunk.chunk_id)

        for chunk in chunks:
            if (chunk.metadata or {}).get("is_context"):
                continue

            prev_id = (chunk.metadata or {}).get("prev_chunk_id")
            next_id = (chunk.metadata or {}).get("next_chunk_id")

            context_ids: List[str] = []
            if prev_id and prev_count > 0 and prev_id not in seen_chunk_ids:
                context_ids.append(prev_id)
            if next_id and next_count > 0 and next_id not in seen_chunk_ids:
                context_ids.append(next_id)

            if not context_ids:
                continue

            effective_index = (chunk.metadata or {}).get("index_name") or index_name
            if not effective_index:
                continue

            try:
                context_chunks = self._fetch_chunks_by_ids(
                    chunk_ids=context_ids,
                    index_name=effective_index,
                    kb_id=kb_id,
                )
                for ctx_chunk in context_chunks:
                    if ctx_chunk.chunk_id in seen_chunk_ids:
                        continue
                    seen_chunk_ids.add(ctx_chunk.chunk_id)
                    ctx_chunk.score = chunk.score * 0.4
                    ctx_chunk.metadata["is_context"] = True
                    ctx_chunk.metadata["context_for_chunk_id"] = chunk.chunk_id
                    if effective_index:
                        ctx_chunk.metadata.setdefault("index_name", effective_index)
                    expanded_chunks.append(ctx_chunk)
            except Exception as exc:
                try:
                    self.logger.warning("Failed to expand context window for %s: %s", chunk.chunk_id, exc)
                except Exception:
                    pass

        return expanded_chunks

    def _fetch_chunks_by_ids(
        self,
        chunk_ids: List[str],
        index_name: str,
        kb_id: int,
    ) -> List[RetrievedChunk]:
        if not chunk_ids:
            return []

        try:
            body = {"ids": chunk_ids}
            response = self.es.es.mget(index=index_name, body=body)  # type: ignore[attr-defined]
        except Exception as exc:
            try:
                self.logger.error("Failed to fetch chunks by IDs: %s", exc)
            except Exception:
                pass
            return []

        chunks: List[RetrievedChunk] = []
        for doc in response.get("docs", []):
            if not doc.get("found"):
                continue
            src = doc.get("_source", {})
            if str(src.get("kb_id")) != str(kb_id):
                continue
            metadata = {
                "kb_id": src.get("kb_id"),
                "document_id": src.get("document_id"),
                "page": src.get("page"),
                "offset_start": src.get("offset_start"),
                "offset_end": src.get("offset_end"),
                "element_type": src.get("element_type"),
                "prev_chunk_id": src.get("prev_chunk_id"),
                "next_chunk_id": src.get("next_chunk_id"),
                "chunk_index": src.get("chunk_index"),
                "section": src.get("section") or src.get("section_type"),
                "section_type": src.get("section_type"),
                "publication_year": src.get("publication_year"),
                "citation_count": src.get("citation_count"),
            }
            if index_name:
                metadata["index_name"] = index_name
            chunks.append(
                RetrievedChunk(
                    chunk_id=doc.get("_id", ""),
                    text=src.get("text", ""),
                    score=0.0,
                    metadata=metadata,
                    source="context",
                )
            )
        return chunks
