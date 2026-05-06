"""Helpers for retrieval statistics."""

from __future__ import annotations

from typing import Any, Dict, List


def build_provider_stats(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize retrieval hits by provider and metadata."""
    stats: Dict[str, Any] = {}
    for chunk in chunks or []:
        metadata = chunk.get("metadata") or {}
        provider = str(metadata.get("rag_provider") or "unknown").lower()
        entry = stats.setdefault(
            provider,
            {
                "chunk_count": 0,
                "doc_count": 0,
                "element_type": {},
                "source": {},
            },
        )
        entry["chunk_count"] += 1

        doc_id = metadata.get("document_id")
        if doc_id is not None:
            doc_set = entry.setdefault("_doc_set", set())
            doc_set.add(str(doc_id))

        element_type = metadata.get("element_type") or "unknown"
        entry["element_type"][element_type] = entry["element_type"].get(element_type, 0) + 1

        source = metadata.get("retrieval_source") or metadata.get("source") or "unknown"
        entry["source"][source] = entry["source"].get(source, 0) + 1

    for entry in stats.values():
        doc_set = entry.pop("_doc_set", set())
        entry["doc_count"] = len(doc_set)

    return stats
