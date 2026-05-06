"""
Retrieval-only facade for the local RAG engine.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from service.core.rag.service import RAGService


class RAGRetriever:
    """Expose retrieval as a standalone tool."""

    def __init__(self, rag_service: Optional[RAGService] = None) -> None:
        """Create a retriever backed by the local RAG service.

        Args:
            rag_service (Optional[RAGService]): Optional shared RAGService instance.
        """
        self._rag = rag_service or RAGService()

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
        """Retrieve chunks using the local RAG engine."""
        return self._rag.retrieve(
            query=query,
            kb_id=kb_id,
            top_k=top_k,
            focus_doc_ids=focus_doc_ids,
            use_vector=use_vector,
            index_override=index_override,
            boost_doc_ids=boost_doc_ids,
            boost_chunk_ids=boost_chunk_ids,
            session_index=session_index,
            index_mode=index_mode,
            provider=provider,
            extra_variants=extra_variants,
        )

    def get_last_retrieval_debug(self) -> Dict[str, Any] | None:
        """Return debug metadata from the last retrieval call."""
        return self._rag.get_last_retrieval_debug()
