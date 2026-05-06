from __future__ import annotations
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import json
import re
import math
import logging
import time
from collections import defaultdict
from pathlib import Path

from core.config import settings
from service.core.rag.retrieval.vector_store import ESVectoreStore, PgVectorStore, RetrieveQuery, RetrievedChunk
from service.core.rag.nlp.model import generate_embedding
from service.core.rag.llm.client import LLMClient

try:
    from elasticsearch import NotFoundError
except ImportError:
    class NotFoundError(RuntimeError):
        """Placeholder used only when the legacy ES dependency is absent."""

        pass


@dataclass
class RAGResult:
    chunks: List[Dict[str, Any]]
    answer: str


class RAGService:
    """Retrieval-only RAG engine for chunk search."""
    def __init__(self) -> None:
        self.store = self._build_vector_store()
        self.llm_aux = LLMClient(task="aux")
        self.logger = logging.getLogger("rag.service")
        self._last_retrieval_debug: Dict[str, Any] | None = None
        self._last_variant_meta: Dict[str, Any] | None = None

    def _build_vector_store(self) -> ESVectoreStore | PgVectorStore:
        vector_store = str(getattr(settings, "SM_VECTOR_STORE", "pgvector") or "pgvector").strip().lower()
        if vector_store == "pgvector":
            return PgVectorStore(default_index=settings.ES_DEFAULT_INDEX)
        return ESVectoreStore(default_index=settings.ES_DEFAULT_INDEX)

    # ------------------------------------------------------------------ #
    # Debug helpers
    # ------------------------------------------------------------------ #
    def _sanitize_metadata(self, metadata: Dict[str, Any] | None) -> Dict[str, Any]:
        if not metadata:
            return {}
        sanitized: Dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                sanitized[key] = value
            elif isinstance(value, (list, dict)):
                sanitized[key] = value
            else:
                sanitized[key] = str(value)
        return sanitized

    def _serialize_chunk_preview(self, chunk: RetrievedChunk, limit: int = 400) -> Dict[str, Any]:
        metadata = self._sanitize_metadata(chunk.metadata or {})
        text_preview = (chunk.text or "")[:limit]
        return {
            "chunk_id": chunk.chunk_id,
            "score": float(chunk.score or 0.0),
            "document_id": metadata.get("document_id"),
            "page": metadata.get("page"),
            "source": chunk.source,
            "element_type": metadata.get("element_type"),
            "logical_type": metadata.get("logical_type"),
            "text_preview": text_preview,
            "metadata": metadata,
        }

    def _serialize_payload_preview(self, payload: Dict[str, Any], limit: int = 400) -> Dict[str, Any]:
        metadata = self._sanitize_metadata(payload.get("metadata") or {})
        text_preview = (payload.get("text") or "")[:limit]
        return {
            "chunk_id": payload.get("chunk_id"),
            "score": float(payload.get("score") or 0.0),
            "document_id": metadata.get("document_id"),
            "page": metadata.get("page"),
            "source": metadata.get("retrieval_source"),
            "element_type": metadata.get("element_type"),
            "logical_type": metadata.get("logical_type"),
            "text_preview": text_preview,
            "metadata": metadata,
        }

    def retrieve(
        self,
        *,
        query: str,
        kb_id: int,
        top_k: int = 5,
        focus_doc_ids: Optional[List[int]] = None,
        use_vector: bool = True,
        index_override: Optional[str] = None,
        boost_doc_ids: Optional[List[int]] = None,
        boost_chunk_ids: Optional[List[str]] = None,
        session_index: Optional[str] = None,
        index_mode: str = "auto",
        provider: Optional[str] = None,
        extra_variants: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        query_policy = self._resolve_query_policy(provider)
        if query_policy.get("mode") == "fast":
            return self._retrieve_fast_path(
                query=query,
                kb_id=kb_id,
                top_k=top_k,
                focus_doc_ids=focus_doc_ids,
                boost_doc_ids=boost_doc_ids,
                session_index=session_index or index_override,
                index_mode=index_mode,
                provider=provider,
                extra_variants=extra_variants,
            )
        return self._retrieve_multi_stage(
            query=query,
            kb_id=kb_id,
            top_k=top_k,
            focus_doc_ids=focus_doc_ids,
            boost_doc_ids=boost_doc_ids,
            boost_chunk_ids=boost_chunk_ids,
            session_index=session_index or index_override,
            index_mode=index_mode,
            provider=provider,
            extra_variants=extra_variants,
        )

    def _retrieve_fast_path(
        self,
        *,
        query: str,
        kb_id: int,
        top_k: int,
        focus_doc_ids: Optional[List[int]],
        boost_doc_ids: Optional[List[int]],
        session_index: Optional[str],
        index_mode: str,
        provider: Optional[str],
        extra_variants: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        query_policy = self._resolve_query_policy(provider)
        variants = self._generate_query_variants(
            query,
            extra_variants=extra_variants,
            enable_translation=query_policy["enable_translation"],
            mq_num_override=query_policy["mq_num"],
            enable_hyde=query_policy["enable_hyde"],
            mode=query_policy["mode"],
        )
        if not variants:
            self._last_retrieval_debug = {
                "strategy": "fast_path",
                "execution_chain": "fast",
                "variants": [],
                "final_chunks": [],
            }
            return []

        variant_cap = max(int(getattr(settings, "SM_FAST_MODE_MAX_VARIANTS", 1) or 1), 1)
        natural_variants = [item for item in variants if not bool(item.get("synthetic"))]
        synthetic_variants = [item for item in variants if bool(item.get("synthetic"))]
        selected_variants = (natural_variants + synthetic_variants)[:variant_cap]
        if self._last_variant_meta is not None:
            self._last_variant_meta["variant_cap"] = variant_cap
            self._last_variant_meta["selected_variants"] = len(selected_variants)

        fast_channels_raw = (
            getattr(settings, "SM_FAST_MODE_RECALL_SOURCES", None)
            or getattr(settings, "SM_RECALL_SOURCES", "bm25,vector")
            or "bm25,vector"
        )
        fast_channels = [seg.strip() for seg in str(fast_channels_raw).split(",") if seg.strip()]
        if not fast_channels:
            fast_channels = ["bm25"]
        vector_enabled = "vector" in {channel.lower() for channel in fast_channels}

        variant_embeddings: Dict[str, List[float]] = {}
        if vector_enabled:
            try:
                texts = [item["text"] for item in selected_variants]
                vecs = generate_embedding(texts)
                if isinstance(vecs, list):
                    for idx, vec in enumerate(vecs):
                        if isinstance(vec, list) and vec:
                            variant_embeddings[texts[idx]] = vec
                if self._last_variant_meta is not None:
                    self._last_variant_meta["embedding_batch"] = len(variant_embeddings)
            except Exception as exc:
                try:
                    self.logger.debug("RAG.retrieve[fast] batch embedding failed: %s", exc)
                except Exception:
                    pass

        mode_alias = {
            "session": "session_only",
            "session_only": "session_only",
            "session-only": "session_only",
            "global": "global_only",
            "global_only": "global_only",
            "global-only": "global_only",
            "both": "hybrid",
            "hybrid": "hybrid",
        }
        normalized_mode = mode_alias.get((index_mode or "auto").strip().lower(), "auto")
        default_index_name = self.store.default_index or settings.ES_DEFAULT_INDEX

        session_index_exists: Optional[bool] = None
        if session_index and normalized_mode in {"auto", "session_only", "hybrid"}:
            session_index_exists = self.store.index_exists(session_index)
            if not session_index_exists:
                session_index = None

        index_plan: List[Dict[str, Optional[str]]] = []
        if normalized_mode == "session_only":
            if session_index:
                index_plan.append({"label": "session", "index": session_index, "fallback": None})
        elif normalized_mode == "global_only":
            index_plan.append({"label": "global", "index": None, "fallback": None})
        elif normalized_mode == "hybrid":
            if session_index:
                index_plan.append({"label": "session", "index": session_index, "fallback": None})
            index_plan.append({"label": "global", "index": None, "fallback": None})
        else:
            if session_index:
                index_plan.append({"label": "session", "index": session_index, "fallback": default_index_name})
            else:
                index_plan.append({"label": "global", "index": None, "fallback": None})
        if not index_plan:
            index_plan.append({"label": "global", "index": None, "fallback": None})

        candidate_multiplier = max(int(getattr(settings, "SM_FAST_MODE_RECALL_MULTIPLIER", 1) or 1), 1)
        channel_topk_cap = max(
            int(getattr(settings, "SM_FAST_MODE_CHANNEL_TOPK", max(top_k, 8)) or max(top_k, 8)),
            top_k,
        )
        sample_limit = max(int(getattr(settings, "SM_DEBUG_PATH_SAMPLE_LIMIT", 5) or 5), 1)

        path_hits: Dict[str, List[RetrievedChunk]] = {}
        flat_hits: List[RetrievedChunk] = []
        total_candidates = 0
        index_stats: Dict[str, int] = defaultdict(int)
        indices_used: set[str] = set()

        for plan in index_plan:
            plan_label = plan.get("label", "global") or "global"
            plan_index = plan.get("index")
            fallback_index = plan.get("fallback")
            for variant in selected_variants:
                rq = RetrieveQuery(
                    text=variant["text"],
                    kb_id=kb_id,
                    top_k=top_k,
                    focus_doc_ids=focus_doc_ids,
                    index_override=plan_index,
                    use_vector=True,
                    channels=fast_channels,
                    query_tag=variant["tag"],
                    synthetic=bool(variant.get("synthetic")),
                    embedding_override=variant_embeddings.get(variant["text"]),
                    boost_doc_ids=boost_doc_ids,
                    fallback_index=fallback_index,
                    channel_topk_cap=channel_topk_cap,
                )
                try:
                    hits = self.store.search_multi_path(
                        query=rq,
                        channels=fast_channels,
                        candidate_multiplier=candidate_multiplier,
                    )
                except NotFoundError:
                    continue
                total_candidates += len(hits)
                index_stats[plan_label] += len(hits)
                for hit in hits:
                    hit.metadata.setdefault("index_label", plan_label)
                    idx_name = hit.metadata.get("index_name")
                    if idx_name:
                        indices_used.add(str(idx_name))
                    path_id = f"{plan_label}:{variant['tag']}::{hit.source}"
                    path_hits.setdefault(path_id, []).append(hit)
                flat_hits.extend(hits)

        if not flat_hits:
            meta = dict(self._last_variant_meta or {})
            meta["channels"] = fast_channels
            if session_index_exists is not None:
                meta["session_index_exists"] = session_index_exists
            self._last_retrieval_debug = {
                "strategy": "fast_path",
                "execution_chain": "fast",
                "variants": selected_variants,
                "path_stats": {},
                "path_samples": [],
                "rrf_candidates": [],
                "rrf_candidates_count": 0,
                "mmr_chunks": [],
                "mmr_output_count": 0,
                "context_chunks": [],
                "final_chunks": [],
                "top_k": top_k,
                "rerank_top_k": top_k,
                "index_mode": normalized_mode,
                "index_plan": index_plan,
                "indices_used": sorted(indices_used),
                "index_stats": dict(index_stats),
                "memory": {"boost_doc_ids": boost_doc_ids or [], "top_doc_id": None, "top_hit": False},
                "graph_boosted_count": 0,
                "graph_boosted_preview": [],
                "multimodal_boosted_count": 0,
                "multimodal_boosted_preview": [],
                "query_meta": meta,
            }
            return []

        best_hits: Dict[str, RetrievedChunk] = {}
        for hit in flat_hits:
            key = str(hit.chunk_id or "")
            if not key:
                continue
            prev = best_hits.get(key)
            if prev is None or float(hit.score or 0.0) > float(prev.score or 0.0):
                best_hits[key] = hit
        ordered_hits = sorted(
            best_hits.values(),
            key=lambda item: float(item.score or 0.0),
            reverse=True,
        )

        fast_rerank_enabled = bool(getattr(settings, "SM_FAST_MODE_RERANK_ENABLED", False))
        rerank_top_k = (
            max(top_k, int(getattr(settings, "SM_L2_RERANK_TOPK", 20) or 20))
            if fast_rerank_enabled
            else top_k
        )
        selected_hits = ordered_hits[:rerank_top_k]
        final_payloads: List[Dict[str, Any]] = []
        for hit in selected_hits:
            metadata = hit.metadata if isinstance(hit.metadata, dict) else {}
            hit.metadata = metadata
            score = float(hit.score or 0.0)
            metadata["fused_score"] = score
            metadata["retrieval_score"] = score
            metadata["retrieval_source"] = metadata.get("retrieval_source") or hit.source
            metadata["fast_mode"] = True
            final_payloads.append(self._chunk_to_payload(chunk=hit, score=score))

        path_samples: List[Dict[str, Any]] = []
        for path_id, hits in path_hits.items():
            prefix, source = path_id.split("::", 1) if "::" in path_id else (path_id, "")
            if ":" in prefix:
                label, query_tag = prefix.split(":", 1)
            else:
                label, query_tag = prefix, "original"
            path_samples.append(
                {
                    "path_id": path_id,
                    "label": label,
                    "query_tag": query_tag,
                    "source": source or None,
                    "hit_count": len(hits),
                    "hits": [self._serialize_chunk_preview(hit) for hit in hits[:sample_limit]],
                }
            )

        final_preview = [self._serialize_payload_preview(payload) for payload in final_payloads]
        top_doc_id = None
        if final_payloads:
            md0 = final_payloads[0].get("metadata") or {}
            top_doc_id = md0.get("document_id")
        memory_debug = {
            "boost_doc_ids": boost_doc_ids or [],
            "top_doc_id": top_doc_id,
            "top_hit": bool(
                top_doc_id is not None and boost_doc_ids and str(top_doc_id) in {str(doc) for doc in boost_doc_ids}
            ),
        }
        preview = [
            {
                "chunk_id": item["chunk_id"],
                "score": round(float(item["score"]), 4),
                "source": item["metadata"].get("retrieval_source"),
            }
            for item in final_payloads[: min(10, len(final_payloads))]
        ]
        meta = dict(self._last_variant_meta or {})
        meta["channels"] = fast_channels
        meta["candidate_multiplier"] = candidate_multiplier
        meta["channel_topk_cap"] = channel_topk_cap
        meta["total_candidates"] = total_candidates
        if session_index_exists is not None:
            meta["session_index_exists"] = session_index_exists

        self._last_retrieval_debug = {
            "strategy": "fast_path",
            "execution_chain": "fast",
            "variants": selected_variants,
            "path_stats": {pth: len(hits) for pth, hits in path_hits.items()},
            "path_samples": path_samples,
            "rrf_candidates": preview,
            "rrf_details": [self._serialize_chunk_preview(hit) for hit in ordered_hits[:sample_limit]],
            "rrf_candidates_count": len(ordered_hits),
            "mmr_chunks": [],
            "mmr_output_count": len(selected_hits),
            "context_chunks": [],
            "final_chunks": final_preview,
            "top_k": top_k,
            "rerank_top_k": rerank_top_k,
            "index_mode": normalized_mode,
            "index_plan": index_plan,
            "indices_used": sorted(indices_used),
            "index_stats": dict(index_stats),
            "memory": memory_debug,
            "graph_boosted_count": 0,
            "graph_boosted_preview": [],
            "multimodal_boosted_count": 0,
            "multimodal_boosted_preview": [],
            "query_meta": meta,
        }
        try:
            self.logger.info(
                f"RAG.retrieve[fast_path] kb={kb_id} variants={len(selected_variants)} "
                f"channels={fast_channels} candidates={len(ordered_hits)} final={len(final_payloads)} "
                f"index_mode={normalized_mode} hyde_fallback={meta.get('hyde_fallback_used')}"
            )
        except Exception:
            pass
        return final_payloads

    def _retrieve_multi_stage(
        self,
        *,
        query: str,
        kb_id: int,
        top_k: int,
        focus_doc_ids: Optional[List[int]],
        boost_doc_ids: Optional[List[int]],
        boost_chunk_ids: Optional[List[str]],
        session_index: Optional[str],
        index_mode: str,
        provider: Optional[str],
        extra_variants: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        query_policy = self._resolve_query_policy(provider)
        variants = self._generate_query_variants(
            query,
            extra_variants=extra_variants,
            enable_translation=query_policy["enable_translation"],
            mq_num_override=query_policy["mq_num"],
            enable_hyde=query_policy["enable_hyde"],
            mode=query_policy["mode"],
        )
        if not variants:
            try:
                self.logger.warning("RAG.retrieve[multi_stage] no query variants generated")
            except Exception:
                pass
            return []

        variant_embeddings: Dict[str, List[float]] = {}
        raw_channels = getattr(settings, "SM_RECALL_SOURCES", "bm25,vector") or "bm25,vector"
        vector_enabled = "vector" in [c.strip() for c in raw_channels.split(",") if c.strip()]
        if vector_enabled:
            try:
                texts = [item["text"] for item in variants]
                vecs = generate_embedding(texts)
                if isinstance(vecs, list):
                    for idx, vec in enumerate(vecs):
                        if isinstance(vec, list) and vec:
                            variant_embeddings[texts[idx]] = vec
                if self._last_variant_meta is not None:
                    self._last_variant_meta["embedding_batch"] = len(variant_embeddings)
            except Exception as exc:
                try:
                    self.logger.debug("RAG.retrieve[multi_stage] batch embedding failed: %s", exc)
                except Exception:
                    pass

        try:
            variant_snapshot = [(item["tag"], len(item["text"])) for item in variants]
            self.logger.debug(
                "RAG.retrieve[multi_stage] query='%s' variants=%s",
                (query or "")[:80],
                variant_snapshot,
            )
        except Exception:
            pass

        mode_alias = {
            "session": "session_only",
            "session_only": "session_only",
            "session-only": "session_only",
            "global": "global_only",
            "global_only": "global_only",
            "global-only": "global_only",
            "both": "hybrid",
            "hybrid": "hybrid",
        }
        normalized_mode = mode_alias.get((index_mode or "auto").strip().lower(), "auto")
        default_index_name = self.store.default_index or settings.ES_DEFAULT_INDEX

        session_index_exists: Optional[bool] = None
        if session_index and normalized_mode in {"auto", "session_only", "hybrid"}:
            session_index_exists = self.store.index_exists(session_index)
            if not session_index_exists:
                session_index = None

        index_plan: List[Dict[str, Optional[str]]] = []
        if normalized_mode == "session_only":
            if session_index:
                index_plan.append({"label": "session", "index": session_index, "fallback": None})
        elif normalized_mode == "global_only":
            index_plan.append({"label": "global", "index": None, "fallback": None})
        elif normalized_mode == "hybrid":
            if session_index:
                index_plan.append({"label": "session", "index": session_index, "fallback": None})
            index_plan.append({"label": "global", "index": None, "fallback": None})
        else:  # auto / legacy
            if session_index:
                index_plan.append({"label": "session", "index": session_index, "fallback": default_index_name})
            else:
                index_plan.append({"label": "global", "index": None, "fallback": None})

        if not index_plan:
            index_plan.append({"label": "global", "index": None, "fallback": None})

        # Hierarchical retrieval: optional document-level pre-filter
        if getattr(settings, "SM_HIERARCHICAL_RETRIEVAL_ENABLED", False) and not focus_doc_ids:
            doc_level_ids = self._hierarchical_doc_prefetch(
                query=query,
                kb_id=kb_id,
                index_plan=index_plan,
                default_index_name=default_index_name,
                variant_embeddings=variant_embeddings,
                variants=variants,
            )
            if doc_level_ids:
                focus_doc_ids = doc_level_ids

        channels = None  # use store defaults
        path_hits: Dict[str, List[RetrievedChunk]] = {}
        total_candidates = 0
        index_stats: Dict[str, int] = defaultdict(int)
        indices_used: set[str] = set()
        sample_limit = max(int(getattr(settings, "SM_DEBUG_PATH_SAMPLE_LIMIT", 5) or 5), 1)

        for plan in index_plan:
            plan_label = plan.get("label", "global") or "global"
            plan_index = plan.get("index")
            fallback_index = plan.get("fallback")

            for variant in variants:
                rq = RetrieveQuery(
                    text=variant["text"],
                    kb_id=kb_id,
                    top_k=max(top_k, 5),
                    focus_doc_ids=focus_doc_ids,
                    index_override=plan_index,
                    use_vector=True,
                    channels=channels,
                    query_tag=variant["tag"],
                    synthetic=variant["synthetic"],
                    embedding_override=variant_embeddings.get(variant["text"]),
                    boost_doc_ids=boost_doc_ids,
                    fallback_index=fallback_index,
                )
                try:
                    hits = self.store.search_multi_path(query=rq)
                except NotFoundError:
                    try:
                        self.logger.warning(
                            "RAG.retrieve[multi_stage] index '%s' (label=%s) not found, skip.",
                            plan_index,
                            plan_label,
                        )
                    except Exception:
                        pass
                    continue

                total_candidates += len(hits)
                index_stats[plan_label] += len(hits)
                for hit in hits:
                    hit.metadata.setdefault("index_label", plan_label)
                    idx_name = hit.metadata.get("index_name")
                    if idx_name:
                        indices_used.add(str(idx_name))
                    path_id = f"{plan_label}:{variant['tag']}::{hit.source}"
                    path_hits.setdefault(path_id, []).append(hit)

                try:
                    self.logger.debug(
                        "RAG.retrieve[multi_stage] label=%s tag=%s hits=%s sources=%s",
                        plan_label,
                        variant["tag"],
                        len(hits),
                        list({hit.source for hit in hits}),
                    )
                except Exception:
                    pass

        ordered_chunks, fused_scores, path_summary = self._rrf_fuse(path_hits)
        path_samples: List[Dict[str, Any]] = []
        for path_id, hits in path_hits.items():
            prefix, source = path_id.split("::", 1) if "::" in path_id else (path_id, "")
            if ":" in prefix:
                label, query_tag = prefix.split(":", 1)
            else:
                label, query_tag = prefix, "original"
            path_samples.append(
                {
                    "path_id": path_id,
                    "label": label,
                    "query_tag": query_tag,
                    "source": source or None,
                    "hit_count": len(hits),
                    "hits": [self._serialize_chunk_preview(hit) for hit in hits[:sample_limit]],
                }
            )
        if not ordered_chunks:
            try:
                self.logger.warning("RAG.retrieve[multi_stage] fusion produced 0 candidates")
            except Exception:
                pass
            return []

        try:
            self.logger.debug(
                "RAG.retrieve[multi_stage] rrf_path_summary=%s",
                {path: len(ids) for path, ids in path_summary.items()},
            )
        except Exception:
            pass

        # 两阶段排序：MMR 输出更多候选给精排，而不是直接输出 top_k
        # 阶段1（粗排）：MMR 输出 SM_L2_RERANK_TOPK 个候选（默认20-30个）
        rerank_top_k = max(
            top_k,
            int(getattr(settings, "SM_L2_RERANK_TOPK", 20) or 20)
        )
        mmr_selected = self._apply_mmr(
            ordered_chunks,
            fused_scores,
            top_k=rerank_top_k,  # MMR 输出更多候选给精排
        )
        mmr_preview = [self._serialize_chunk_preview(chunk) for chunk in mmr_selected[:sample_limit]]
        rrf_details = [self._serialize_chunk_preview(chunk) for chunk in ordered_chunks[:sample_limit]]

        try:
            mmr_preview = [chunk.chunk_id for chunk in mmr_selected[: min(10, len(mmr_selected))]]
            self.logger.debug(
                "RAG.retrieve[multi_stage] mmr_selected_preview=%s",
                mmr_preview,
            )
        except Exception:
            pass

        if len(index_plan) == 1:
            primary_index_hint = index_plan[0].get("index") or default_index_name
        else:
            primary_index_hint = None
        if getattr(settings, "SM_EQUATION_CONTEXT_EXPANSION", True):
            context_augmented = self.store._expand_equation_context(  # type: ignore[attr-defined]
                chunks=mmr_selected,
                index_name=primary_index_hint,
                kb_id=kb_id,
            )
            # `_expand_equation_context` 会返回原始块 + 上下文块，需剔除重复
            base_ids = {chunk.chunk_id for chunk in mmr_selected}
            augmented: List[RetrievedChunk] = []
            seen: set[str] = set()
            for chunk in context_augmented:
                if chunk.chunk_id in seen:
                    continue
                seen.add(chunk.chunk_id)
                # 对上下文块若不存在 RRF 分数，使用自身得分
                if chunk.chunk_id not in fused_scores:
                    fused_scores[chunk.chunk_id] = float(chunk.score or 0.0)
                augmented.append(chunk)
            mmr_selected = [c for c in augmented if c.chunk_id in base_ids]
            context_only = [c for c in augmented if c.chunk_id not in base_ids]
        else:
            context_only = []
        context_preview = [self._serialize_chunk_preview(chunk) for chunk in context_only[:sample_limit]]

        # 通用上下文窗口扩展（对非公式块也附带前后邻居）
        if getattr(settings, "SM_CONTEXT_WINDOW_EXPANSION_ENABLED", False):
            win_augmented = self.store._expand_context_window(  # type: ignore[attr-defined]
                chunks=mmr_selected + context_only,
                index_name=primary_index_hint,
                kb_id=kb_id,
            )
            win_base_ids = {c.chunk_id for c in mmr_selected}
            win_ctx_ids = {c.chunk_id for c in context_only}
            win_new_ctx = [
                c for c in win_augmented
                if c.chunk_id not in win_base_ids and c.chunk_id not in win_ctx_ids
            ]
            for wc in win_new_ctx:
                if wc.chunk_id not in fused_scores:
                    fused_scores[wc.chunk_id] = float(wc.score or 0.0)
            context_only = context_only + win_new_ctx

        # 阶段2（精排）：在 MMR 输出的候选上进行元数据处理，准备给精排
        # 注意：精排会在外部（session_rt.py/debug_rt.py）进行，这里只准备候选
        metadata_stage_chunks = self._apply_metadata_stage(
            mmr_selected,
            fused_scores,
            boost_chunk_ids=boost_chunk_ids,
            provider=provider,
        )
        
        # 追加上下文块（保持原序，降低权重）
        for ctx in context_only:
            ctx_payload = {
                "text": ctx.text,
                "metadata": ctx.metadata,
                "score": float(ctx.score or fused_scores.get(ctx.chunk_id, 0.0)),
                "chunk_id": ctx.chunk_id,
            }
            if "retrieval_source" not in ctx_payload["metadata"]:
                ctx_payload["metadata"]["retrieval_source"] = ctx.source
            ctx_payload["metadata"].setdefault("is_context", True)
            metadata_stage_chunks.append(ctx_payload)

        # RL 阶段（如果启用）
        metadata_stage_chunks = self._apply_rl_stage(question=query, payloads=metadata_stage_chunks)
        
        # 最终输出：返回所有 MMR 输出的候选供精排
        # 注意：精排后的 top_k 选择会在 session_rt.py/debug_rt.py 中进行
        # 这里不取 top_k，而是返回所有候选（rerank_top_k 个）给精排
        final_payloads = metadata_stage_chunks  # 返回所有候选供精排
        final_preview = [self._serialize_payload_preview(payload) for payload in final_payloads]
        graph_boosted_preview: List[str] = []
        graph_boosted_count = 0
        if boost_chunk_ids:
            boost_set = {str(cid) for cid in boost_chunk_ids}
            for payload in metadata_stage_chunks:
                if payload.get("chunk_id") in boost_set:
                    graph_boosted_count += 1
                    if len(graph_boosted_preview) < 10:
                        graph_boosted_preview.append(payload.get("chunk_id"))

        multimodal_boosted_preview: List[str] = []
        multimodal_boosted_count = 0
        for payload in metadata_stage_chunks:
            md = payload.get("metadata") or {}
            if md.get("multimodal_boost"):
                multimodal_boosted_count += 1
                if len(multimodal_boosted_preview) < 10:
                    multimodal_boosted_preview.append(payload.get("chunk_id"))

        try:
            metadata_preview = [item.get("chunk_id") for item in metadata_stage_chunks[: min(10, len(metadata_stage_chunks))]]
            self.logger.debug(
                "RAG.retrieve[multi_stage] metadata_stage_preview=%s context_added=%s mmr_output=%s final_output=%s",
                metadata_preview,
                len(context_only),
                len(mmr_selected),
                len(final_payloads),
            )
        except Exception:
            pass

        try:
            self.logger.info(
                f"RAG.retrieve[multi_stage] kb={kb_id} variants={len(variants)} paths={len(path_hits)} "
                f"candidates={total_candidates} rrf={len(ordered_chunks)} mmr={len(mmr_selected)} "
                f"final={len(final_payloads)} index_mode={normalized_mode} "
                f"hyde_fallback={(self._last_variant_meta or {}).get('hyde_fallback_used')}"
            )
        except Exception:
            pass

        try:
            top_doc_id = None
            if final_payloads:
                md0 = final_payloads[0].get("metadata") or {}
                top_doc_id = md0.get("document_id")
            memory_debug = {
                "boost_doc_ids": boost_doc_ids or [],
                "top_doc_id": top_doc_id,
                "top_hit": bool(top_doc_id is not None and boost_doc_ids and str(top_doc_id) in {str(doc) for doc in boost_doc_ids}),
            }
            preview = [
                {
                    "chunk_id": item["chunk_id"],
                    "score": round(float(item["score"]), 4),
                    "source": item["metadata"].get("retrieval_source"),
                }
                for item in final_payloads[: min(10, len(final_payloads))]
            ]
            meta = dict(self._last_variant_meta or {})
            if session_index_exists is not None:
                meta["session_index_exists"] = session_index_exists
            self._last_retrieval_debug = {
                "strategy": "deep_multi_stage",
                "execution_chain": "deep",
                "variants": variants,
                "path_stats": {pth: len(hits) for pth, hits in path_hits.items()},
                "rrf_candidates": preview,
                "rrf_details": rrf_details,
                "rrf_candidates_count": len(ordered_chunks),  # RRF 融合后的实际候选数
                "path_samples": path_samples,
                "mmr_chunks": mmr_preview,
                "mmr_output_count": len(mmr_selected),  # MMR 输出的候选数（给精排的）
                "context_chunks": context_preview,
                "final_chunks": final_preview,
                "top_k": top_k,
                "rerank_top_k": rerank_top_k,  # 精排候选数（MMR输出数）
                "index_mode": normalized_mode,
                "index_plan": index_plan,
                "indices_used": sorted(indices_used),
                "index_stats": dict(index_stats),
                "memory": memory_debug,
                "graph_boosted_count": graph_boosted_count,
                "graph_boosted_preview": graph_boosted_preview,
                "multimodal_boosted_count": multimodal_boosted_count,
                "multimodal_boosted_preview": multimodal_boosted_preview,
                "query_meta": meta,
            }
        except Exception:
            self._last_retrieval_debug = None

        return final_payloads

    # --- query generation helpers -------------------------------------------------
    def _resolve_query_policy(self, provider: Optional[str]) -> Dict[str, Any]:
        provider_norm = (provider or "").strip().lower()
        is_deep = provider_norm in {"graph", "multimodal_graph"}
        if is_deep:
            return {
                "mode": "deep",
                "enable_translation": bool(getattr(settings, "SM_AUTO_TRANSLATE_TO_EN", True)),
                "mq_num": int(getattr(settings, "SM_MULTI_QUERY_NUM", 1) or 1),
                "enable_hyde": bool(getattr(settings, "SM_HYDE_ENABLED", True)),
            }
        return {
            "mode": "fast",
            "enable_translation": bool(getattr(settings, "SM_FAST_MODE_AUTO_TRANSLATE", False)),
            "mq_num": int(getattr(settings, "SM_FAST_MODE_MQ_NUM", 1) or 1),
            "enable_hyde": bool(getattr(settings, "SM_FAST_MODE_HYDE_ENABLED", False)),
        }

    def _generate_query_variants(
        self,
        query: str,
        *,
        extra_variants: Optional[List[Dict[str, Any]]] = None,
        enable_translation: Optional[bool] = None,
        mq_num_override: Optional[int] = None,
        enable_hyde: Optional[bool] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        original_query = (query or "").strip()
        if not original_query:
            return []

        contains_cjk = self._contains_cjk(original_query)
        effective_query = original_query
        translation_used = False

        allow_translate = (
            enable_translation
            if enable_translation is not None
            else getattr(settings, "SM_AUTO_TRANSLATE_TO_EN", True)
        )
        if contains_cjk and allow_translate:
            translated = self._translate_to_english(original_query)
            cleaned = self._clean_query_text(translated)
            if cleaned and cleaned != original_query:
                effective_query = cleaned
                translation_used = True
                try:
                    self.logger.debug(
                        "Auto translated query zh->en: '%s' -> '%s'",
                        original_query,
                        effective_query,
                    )
                except Exception:
                    pass

        target_language = "en" if translation_used or not contains_cjk else settings.SM_DEFAULT_LANGUAGE

        variants: List[Dict[str, Any]] = [
            {"text": effective_query, "tag": "original", "synthetic": False, "language": target_language},
        ]

        mq_num = int(mq_num_override) if mq_num_override is not None else int(getattr(settings, "SM_MULTI_QUERY_NUM", 1) or 1)
        mq_num = max(mq_num, 1)
        if mq_num_override is not None:
            mq_cap = max(mq_num, 1)
        else:
            mq_cap = max(int(getattr(settings, "SM_MULTI_QUERY_MAX", mq_num) or mq_num), mq_num)
        mq_num = min(mq_num, mq_cap)
        if mq_num > 1:
            rewrites = self._rewrite_queries(effective_query, mq_num - 1)
            for idx, text in enumerate(rewrites, start=1):
                variants.append(
                    {
                        "text": text,
                        "tag": f"mq_{idx}",
                        "synthetic": False,
                        "language": target_language,
                    }
                )

        allow_hyde = (
            enable_hyde
            if enable_hyde is not None
            else getattr(settings, "SM_HYDE_ENABLED", True)
        )
        hyde_key_terms = self._extract_keywords(effective_query, limit=5)
        hyde_word_limit = max(int(getattr(settings, "SM_HYDE_WORD_LIMIT", 90) or 90), 40)
        hyde_generated = False
        hyde_fallback_used = False
        if allow_hyde:
            hyde_text = self._generate_hyde_document(
                effective_query,
                language=target_language,
            )
            if not hyde_text and bool(getattr(settings, "SM_HYDE_FALLBACK_ENABLED", True)):
                hyde_text = self._build_hyde_fallback(
                    query=effective_query,
                    language=target_language,
                    key_terms=hyde_key_terms,
                    word_limit=hyde_word_limit,
                )
                hyde_fallback_used = bool(hyde_text)
            if hyde_text:
                variants.append({"text": hyde_text, "tag": "hyde", "synthetic": True, "language": target_language})
                hyde_generated = True

        extra_payloads: List[Dict[str, Any]] = []
        for item in extra_variants or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            extra_payloads.append(
                {
                    "text": text,
                    "tag": item.get("tag") or "extra",
                    "synthetic": bool(item.get("synthetic", True)),
                    "language": item.get("language") or target_language,
                }
            )
        variants.extend(extra_payloads)

        dedup: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in variants:
            key = item["text"].strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            dedup.append(item)

        cap = max(mq_cap, 1 + len(extra_payloads))
        if len(dedup) > cap:
            dedup = dedup[:cap]
        self._last_variant_meta = {
            "original_query": original_query,
            "effective_query": effective_query,
            "translation_used": translation_used,
            "target_language": target_language,
            "mode": mode,
            "mq_num": mq_num,
            "hyde_enabled": bool(allow_hyde),
            "hyde_generated": hyde_generated,
            "hyde_fallback_used": hyde_fallback_used,
            "translation_enabled": bool(allow_translate),
            "extra_variants": len(extra_payloads),
        }
        try:
            self.logger.info(
                f"RAG.query_variants mode={mode or 'unknown'} translation_used={translation_used} "
                f"mq_num={mq_num} hyde_enabled={bool(allow_hyde)} hyde_generated={hyde_generated} "
                f"hyde_fallback={hyde_fallback_used} total={len(dedup)}"
            )
        except Exception:
            pass
        return dedup

    def _rewrite_queries(self, query: str, extra: int) -> List[str]:
        if extra <= 0:
            return []
        focuses = self._build_rewrite_focuses(query, extra)
        keywords = self._extract_keywords(query, limit=6)
        focus_text = "\n".join(
            f"- {item['id']}: {item['instruction']}" for item in focuses
        )
        user_prompt = (
            "Original research question:\n"
            f"{query}\n\n"
            "Craft a concise English search query for each focus below. "
            "Each query must explicitly reference the same domain and, when possible, reuse the key terms. "
            "Return a JSON array where every element is {\"focus\": \"id\", \"query\": \"...\"}.\n"
            f"Key terms to preserve: {', '.join(keywords) if keywords else 'use the existing technical terms from the question.'}\n"
            f"Focus list:\n{focus_text}"
        )
        prompts = [
            {
                "role": "system",
                "content": (
                    "You rewrite research questions into diverse academic search queries. "
                    "Respect the provided focus per query, output JSON only, no explanations."
                ),
            },
            {"role": "user", "content": user_prompt},
        ]
        json_payload: Optional[str]
        try:
            json_payload = self.llm_aux.generate(prompts, temperature=0.1, max_tokens=256, stream=False)
        except Exception:
            json_payload = None

        results: List[str] = []
        if json_payload:
            try:
                parsed = json.loads(json_payload)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("query") or "").strip()
                    if text:
                        results.append(text)

        if not results:
            # Fallback to simple line-based parsing
            simple_resp = json_payload or ""
            for line in simple_resp.splitlines():
                stripped = line.strip(" -•\t").strip()
                if stripped:
                    results.append(stripped)

        return results[:extra]

    def _generate_hyde_document(self, query: str, *, language: Optional[str] = None) -> str | None:
        target_language = (language or settings.SM_DEFAULT_LANGUAGE or "zh").lower()
        key_terms = self._extract_keywords(query, limit=5)
        instruction_terms = ", ".join(key_terms) if key_terms else "the same domain terminology from the question"
        word_limit = max(int(getattr(settings, "SM_HYDE_WORD_LIMIT", 90) or 90), 40)
        system_prompt = (
            "You write hypothetical abstracts for academic search."
            if target_language == "en"
            else "你是一名学术摘要助手，负责在相同领域内生成假设性摘要。"
        )
        user_prompt = (
            f"Research question: {query}\n"
            f"Key terms that MUST appear verbatim: {instruction_terms}\n"
            "Write 3 sentences describing (1) the type of solution/framework, (2) the technical components, "
            "and (3) the concrete problems or constraints it addresses. "
            "Stay strictly within the domain implied by the key terms. "
            f"Limit the response to about {word_limit} words."
        )
        prompts = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            hyde = self.llm_aux.generate(
                prompts,
                temperature=float(getattr(settings, "SM_HYDE_TEMPERATURE", 0.2) or 0.2),
                max_tokens=int(getattr(settings, "SM_HYDE_MAX_TOKENS", 256) or 256),
                stream=False,
            )
        except Exception as exc:
            try:
                self.logger.warning(f"RAG.hyde_generate_failed query='{query[:120]}' err={exc}")
            except Exception:
                pass
            hyde = None
        hyde = self._sanitize_hyde_text(hyde or "", word_limit=word_limit)
        if not hyde:
            return None
        if not self._validate_hyde_text(hyde, key_terms):
            return None
        return hyde

    def _build_hyde_fallback(
        self,
        *,
        query: str,
        language: Optional[str],
        key_terms: List[str],
        word_limit: int,
    ) -> str | None:
        terms = [term for term in (key_terms or []) if term][:4]
        if (language or "").lower() == "zh":
            if terms:
                draft = (
                    f"该研究围绕“{query}”展开，核心技术涉及 {', '.join(terms)} 等模块。"
                    "方法通常结合建模、优化与推理流程，并在真实约束下改进稳定性和效率。"
                    "该方向关注复杂场景下的性能、泛化与可部署性。"
                )
            else:
                draft = (
                    f"该研究围绕“{query}”展开，提出可落地的技术框架与关键模块。"
                    "方法强调在复杂约束条件下提升鲁棒性、效率与准确性。"
                    "该方向关注真实场景中的性能优化与工程可用性。"
                )
        else:
            preserved = ", ".join(terms) if terms else query
            draft = (
                f"This study investigates {query}. "
                f"It proposes a practical framework combining {preserved} with robust optimization and inference steps. "
                "The approach targets performance, generalization, and deployment constraints in realistic settings."
            )
        sanitized = self._sanitize_hyde_text(draft, word_limit=word_limit)
        if not sanitized:
            return None
        # Fallback 文本不强制关键字全命中，避免全部被拒绝。
        return sanitized

    def _build_rewrite_focuses(self, query: str, extra: int) -> List[Dict[str, str]]:
        templates = [
            {
                "id": "methodology",
                "instruction": "Highlight the solution family, architecture, or algorithm class proposed to answer the question.",
            },
            {
                "id": "components",
                "instruction": "Emphasize the core technical components, such as specific models, encoders, or optimization techniques.",
            },
            {
                "id": "problems",
                "instruction": "Target the concrete challenges, constraints, or objectives the solution addresses.",
            },
            {
                "id": "scenario",
                "instruction": "Mention the application scenario, dataset, or environment (e.g., IoV, edge computing, DAG scheduling).",
            },
            {
                "id": "outcomes",
                "instruction": "Focus on the measurable outcomes or benefits such as latency reduction, reliability, or accuracy.",
            },
        ]
        return templates[: max(0, min(extra, len(templates)))]

    def _extract_keywords(self, text: str, limit: int = 6) -> List[str]:
        if not text:
            return []
        stopwords = {
            "what",
            "which",
            "kind",
            "type",
            "does",
            "do",
            "and",
            "the",
            "a",
            "an",
            "of",
            "in",
            "is",
            "are",
            "for",
            "primarily",
            "primary",
            "address",
            "addresses",
            "problem",
            "problems",
            "solution",
            "solutions",
            "main",
            "major",
            "task",
            "tasks",
        }
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text)
        keywords: List[str] = []
        seen: set[str] = set()
        for token in tokens:
            norm = token.lower()
            if len(norm) < 3:
                continue
            if norm in stopwords:
                continue
            if norm in seen:
                continue
            seen.add(norm)
            keywords.append(token)
            if len(keywords) >= limit:
                break
        return keywords

    def _sanitize_hyde_text(self, text: str, *, word_limit: int) -> str:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        if not cleaned:
            return ""
        words = cleaned.split()
        if len(words) > word_limit:
            cleaned = " ".join(words[:word_limit])
        return cleaned

    def _validate_hyde_text(self, text: str, required_terms: List[str]) -> bool:
        if not text:
            return False
        lowered = text.lower()
        checks = required_terms[:3]
        for term in checks:
            if term and term.lower() not in lowered:
                return False
        return True

    def _contains_cjk(self, text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    def _translate_to_english(self, text: str) -> str:
        prompts = [
            {
                "role": "system",
                "content": "You are a professional translator. Translate the user's question into fluent academic English. Output only the translation without explanations.",
            },
            {"role": "user", "content": text},
        ]
        try:
            translated = self.llm_aux.generate(
                prompts,
                temperature=0.0,
                max_tokens=256,
                stream=False,
            )
            return translated or text
        except Exception as exc:
            try:
                self.logger.warning("Auto translation failed: %s", exc)
            except Exception:
                pass
            return text

    def _clean_query_text(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"^[-•\s]+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned

    # --- fusion & ranking --------------------------------------------------------
    def _rrf_fuse(
        self,
        path_hits: Dict[str, List[RetrievedChunk]],
    ) -> tuple[List[RetrievedChunk], Dict[str, float], Dict[str, List[str]]]:
        if not path_hits:
            return [], {}, {}
        k_val = max(int(getattr(settings, "SM_RRF_K", 60) or 60), 1)
        scores: Dict[str, float] = defaultdict(float)
        best_chunk: Dict[str, RetrievedChunk] = {}
        summary: Dict[str, List[str]] = {}

        for path_id, hits in path_hits.items():
            hits_sorted = sorted(hits, key=lambda h: h.rank or 10**6)
            summary[path_id] = [h.chunk_id for h in hits_sorted[: min(10, len(hits_sorted))]]
            for idx, hit in enumerate(hits_sorted, start=1):
                scores[hit.chunk_id] += 1.0 / (k_val + idx)
                if hit.chunk_id not in best_chunk or hit.score > best_chunk[hit.chunk_id].score:
                    best_chunk[hit.chunk_id] = hit

        ordered_ids = sorted(best_chunk.keys(), key=lambda cid: scores[cid], reverse=True)
        ordered_chunks = [best_chunk[cid] for cid in ordered_ids]
        return ordered_chunks, scores, summary

    def _apply_mmr(
        self,
        candidates: List[RetrievedChunk],
        fused_scores: Dict[str, float],
        *,
        top_k: int,
    ) -> List[RetrievedChunk]:
        if not candidates:
            return []
        if not getattr(settings, "SM_MMR_ENABLED", True):
            return candidates[:top_k]

        lambda_val = float(getattr(settings, "SM_MMR_LAMBDA", 0.65) or 0.65)
        max_candidates = max(int(getattr(settings, "SM_MMR_MAX_CANDIDATES", 60) or 60), top_k)
        pool = candidates[:max_candidates]
        selected: List[RetrievedChunk] = []

        while pool and len(selected) < top_k:
            best_candidate = None
            best_score = float("-inf")
            for cand in pool:
                relevance = fused_scores.get(cand.chunk_id, float(cand.score or 0.0))
                if not selected:
                    mmr_score = relevance
                else:
                    max_sim = max(self._chunk_similarity(cand, s) for s in selected)
                    mmr_score = lambda_val * relevance - (1 - lambda_val) * max_sim
                if mmr_score > best_score:
                    best_candidate = cand
                    best_score = mmr_score
            if best_candidate is None:
                break
            selected.append(best_candidate)
            pool = [c for c in pool if c.chunk_id != best_candidate.chunk_id]

        if len(selected) < top_k:
            remaining = [c for c in candidates if all(c.chunk_id != s.chunk_id for s in selected)]
            selected.extend(remaining[: max(0, top_k - len(selected))])
        return selected

    def _chunk_similarity(self, a: RetrievedChunk, b: RetrievedChunk) -> float:
        ta = self._token_set(a.text)
        tb = self._token_set(b.text)
        if not ta or not tb:
            return 0.0
        intersection = len(ta & tb)
        union = len(ta | tb)
        if union == 0:
            return 0.0
        return intersection / union

    def _token_set(self, text: str) -> set[str]:
        tokens = re.split(r"[^\w]+", text.lower())
        return {t for t in tokens if t}

    def _hierarchical_doc_prefetch(
        self,
        *,
        query: str,
        kb_id: int,
        index_plan: List[Dict[str, Optional[str]]],
        default_index_name: str,
        variant_embeddings: Dict[str, List[float]],
        variants: List[Dict[str, Any]],
    ) -> Optional[List[int]]:
        """Document-level pre-retrieval: search level=document records first, return top doc IDs."""
        try:
            primary_variant = next((v for v in variants if v.get("tag") == "original"), None) or (variants[0] if variants else None)
            if not primary_variant:
                return None

            plan_index = index_plan[0].get("index") if index_plan else None
            effective_index = plan_index or default_index_name

            rq = RetrieveQuery(
                text=primary_variant["text"],
                kb_id=kb_id,
                top_k=3,
                focus_doc_ids=None,
                index_override=plan_index,
                use_vector=True,
                channels=None,
                query_tag="hierarchical_doc",
                synthetic=False,
                embedding_override=variant_embeddings.get(primary_variant["text"]),
            )

            es = getattr(self.store, "es", None)
            if es is None:
                return None

            body: dict = {
                "size": 3,
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"kb_id": str(kb_id)}},
                            {"term": {"level": "document"}},
                        ],
                    }
                },
                "_source": ["document_id"],
            }

            embedding = rq.embedding_override
            if embedding:
                body["knn"] = {
                    "field": "vector",
                    "query_vector": embedding,
                    "k": 3,
                    "num_candidates": 10,
                    "filter": {
                        "bool": {
                            "must": [
                                {"term": {"kb_id": str(kb_id)}},
                                {"term": {"level": "document"}},
                            ]
                        }
                    },
                }

            resp = es.es.search(index=effective_index, body=body)
            hits = resp.get("hits", {}).get("hits", [])
            doc_ids = []
            for hit in hits:
                src = hit.get("_source", {})
                did = src.get("document_id")
                if did is not None:
                    try:
                        doc_ids.append(int(did))
                    except (ValueError, TypeError):
                        pass
            if doc_ids:
                try:
                    self.logger.info(
                        "[HIERARCHICAL_PREFETCH] found %d doc(s): %s",
                        len(doc_ids), doc_ids,
                    )
                except Exception:
                    pass
                return doc_ids
        except Exception as exc:
            try:
                self.logger.debug("Hierarchical doc prefetch skipped: %s", exc)
            except Exception:
                pass
        return None

    def _apply_metadata_stage(
        self,
        chunks: List[RetrievedChunk],
        fused_scores: Dict[str, float],
        *,
        boost_chunk_ids: Optional[List[str]] = None,
        provider: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not chunks:
            return []

        current_year = time.localtime().tm_year
        recency_weight = float(getattr(settings, "SM_METADATA_WEIGHT_RECENCY", 0.0) or 0.0)
        citation_weight = float(getattr(settings, "SM_METADATA_WEIGHT_CITATIONS", 0.0) or 0.0)
        section_bonus = float(getattr(settings, "SM_METADATA_SECTION_BONUS", 0.0) or 0.0)
        priority_raw = getattr(settings, "SM_METADATA_SECTION_PRIORITY", "") or ""
        priority_order = [seg.strip().casefold() for seg in priority_raw.split(":") if seg.strip()]
        priority_map = {name: (len(priority_order) - idx) / max(len(priority_order), 1) for idx, name in enumerate(priority_order)}

        boost_set = {str(cid) for cid in boost_chunk_ids or [] if cid}
        provider_norm = (provider or "").strip().lower()
        enable_multimodal_boost = provider_norm in {"multimodal_graph"}
        table_boost = float(getattr(settings, "SM_MULTIMODAL_TABLE_BOOST", 0.25) or 0.25)
        equation_boost = float(getattr(settings, "SM_MULTIMODAL_EQUATION_BOOST", 0.3) or 0.3)
        figure_boost = float(getattr(settings, "SM_MULTIMODAL_FIGURE_BOOST", 0.2) or 0.2)
        logical_raw = getattr(settings, "SM_MULTIMODAL_LOGICAL_PRIORITY", "") or ""
        logical_order = [seg.strip().casefold() for seg in logical_raw.split(":") if seg.strip()]
        logical_map = {name: (len(logical_order) - idx) / max(len(logical_order), 1) for idx, name in enumerate(logical_order)}
        logical_boost = float(getattr(settings, "SM_MULTIMODAL_LOGICAL_BOOST", 0.2) or 0.2)
        reference_boost = float(getattr(settings, "SM_MULTIMODAL_REFERENCE_BOOST", 0.15) or 0.15)
        graph_boost_weight = float(getattr(settings, "SM_GRAPH_CHUNK_BOOST_WEIGHT", 0.35) or 0.35)
        enriched: List[tuple[float, RetrievedChunk]] = []
        for chunk in chunks:
            md = chunk.metadata or {}
            score = fused_scores.get(chunk.chunk_id, float(chunk.score or 0.0))

            year = md.get("publication_year")
            if isinstance(year, str) and year.isdigit():
                year = int(year)
            if isinstance(year, int):
                span = max(current_year - 1970, 1)
                score += recency_weight * max(0.0, (year - 1970) / span)

            citations = md.get("citation_count")
            if isinstance(citations, str) and citations.isdigit():
                citations = int(citations)
            if isinstance(citations, int) and citations > 0:
                score += citation_weight * math.log1p(citations)

            section = (md.get("section") or md.get("section_type") or "").casefold()
            if section_bonus and section in priority_map:
                score += section_bonus * priority_map.get(section, 0.0)

            if boost_set and str(chunk.chunk_id) in boost_set:
                score += graph_boost_weight
                md["graph_boost"] = True

            if enable_multimodal_boost:
                element_type = str(md.get("element_type") or "").lower()
                logical_type = str(md.get("logical_type") or "").casefold()
                section_type = str(md.get("section") or md.get("section_type") or "").casefold()
                if element_type == "table_json":
                    score += table_boost
                    md["multimodal_boost"] = True
                elif element_type == "equation_latex":
                    score += equation_boost
                    md["multimodal_boost"] = True
                elif element_type == "figure_summary":
                    score += figure_boost
                    md["multimodal_boost"] = True
                if logical_map:
                    logical_key = logical_type or section_type
                    if logical_key in logical_map:
                        score += logical_boost * logical_map.get(logical_key, 0.0)
                        md["multimodal_logical_boost"] = True
                if section_type in {"references", "reference", "bibliography"} or logical_type in {"references", "reference", "bibliography"}:
                    score += reference_boost
                    md["multimodal_reference_boost"] = True

            md["fused_score"] = fused_scores.get(chunk.chunk_id, float(chunk.score or 0.0))
            md["retrieval_score"] = score
            md["retrieval_source"] = md.get("retrieval_source") or chunk.source
            enriched.append((score, chunk))

        enriched.sort(key=lambda item: item[0], reverse=True)
        payloads: List[Dict[str, Any]] = []
        for score, chunk in enriched:
            payloads.append(self._chunk_to_payload(chunk=chunk, score=score))
        return payloads

    def _chunk_to_payload(self, *, chunk: RetrievedChunk, score: float) -> Dict[str, Any]:
        return {
            "text": chunk.text,
            "metadata": chunk.metadata,
            "score": float(score),
            "chunk_id": chunk.chunk_id,
        }

    def _apply_rl_stage(self, *, question: str, payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not getattr(settings, "SM_L3_RL_ENABLED", False):
            return payloads
        try:
            self._record_rl_event(question=question, payloads=payloads)
        except Exception:
            pass
        return payloads

    def _record_rl_event(self, *, question: str, payloads: List[Dict[str, Any]]) -> None:
        buffer_path = Path(getattr(settings, "SM_RL_EVENT_BUFFER", "storage/rl_events.jsonl"))
        buffer_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": int(time.time()),
            "question": question,
            "candidates": [
                {
                    "chunk_id": item.get("chunk_id"),
                    "score": item.get("score"),
                    "document_id": (item.get("metadata") or {}).get("document_id"),
                    "page": (item.get("metadata") or {}).get("page"),
                    "source": (item.get("metadata") or {}).get("retrieval_source"),
                }
                for item in payloads
            ],
        }
        with buffer_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")

    def get_last_retrieval_debug(self) -> Dict[str, Any] | None:
        return self._last_retrieval_debug

    # --- citations helper ---
    def build_citations(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Aggregate chunks into citation objects with sufficient metadata for UI navigation."""
        citations: List[Dict[str, Any]] = []
        for c in chunks:
            md = c.get("metadata", {}) or {}
            text = (c.get("text") or c.get("content") or "").strip()
            doc_id = md.get("document_id")
            doc_name = (
                md.get("document_title")
                or md.get("title")
                or md.get("document_name")
                or (f"文档 {doc_id}" if doc_id else "未命名文档")
            )
            
            # 构建 positions 字段：从 bbox_list 或 page_range 推导
            positions = md.get("positions")
            if not positions:
                bbox_list = md.get("bbox_list")
                page = md.get("page")
                page_range = md.get("page_range")
                
                if bbox_list and isinstance(bbox_list, list) and len(bbox_list) > 0:
                    # 从 bbox_list 提取页内位置（使用第一个 bbox 的 y0 坐标）
                    first_bbox = bbox_list[0]
                    if isinstance(first_bbox, (list, tuple)) and len(first_bbox) >= 2:
                        y_pos = int(first_bbox[1]) if isinstance(first_bbox[1], (int, float)) else 0
                        if page is not None:
                            positions = [[int(page), y_pos]]
                        elif page_range and len(page_range) > 0:
                            positions = [[int(page_range[0]), y_pos]]
                elif page_range and isinstance(page_range, list) and len(page_range) > 0:
                    # 使用 page_range 构建 positions
                    positions = [[int(p)] for p in page_range if isinstance(p, (int, float))]
                elif page is not None:
                    # 只有 page，没有具体位置
                    positions = [[int(page)]]
                else:
                    positions = []
            
            citations.append(
                {
                    "id": c.get("chunk_id") or md.get("chunk_id") or md.get("id"),
                    "document_id": doc_id,
                    "document_name": doc_name,
                    "document_title": md.get("document_title"),
                    "doi": md.get("doi"),
                    "knowledge_base_id": md.get("knowledge_base_id") or md.get("kb_id"),
                    "page": md.get("page"),
                    "chunk_id": c.get("chunk_id"),
                    "score": c.get("score"),
                    "snippet": text[:300],
                    "source_text": text,
                    "positions": positions,
                    "page_range": md.get("page_range"),
                    "element_type": md.get("element_type") or md.get("type"),
                    "logical_type": md.get("logical_type"),
                    "structure_title": md.get("structure_title"),
                    "structure_path": md.get("structure_path"),
                    "structure_chunk_index": md.get("structure_chunk_index"),
                    "structure_chunk_total": md.get("structure_chunk_total"),
                    "bbox_list": md.get("bbox_list"),
                    "offsets": {
                        "start": md.get("offset_start", 0),
                        "end": md.get("offset_end", 0),
                    },
                    "alignment_status": md.get("alignment_status"),
                    "source": md.get("source"),
                    "parser_engine": md.get("parser_engine"),
                }
            )
        return citations

