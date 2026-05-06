"""Utility helpers for chat orchestration."""

from __future__ import annotations

from typing import Optional

from core.config import settings


def normalize_top_k(value: Optional[int]) -> int:
    """Normalize top_k within configured bounds.

    Args:
        value (Optional[int]): Requested top_k value.

    Returns:
        int: Normalized top_k within min/max bounds.
    """
    min_k = max(1, getattr(settings, "SM_RAG_TOPK_MIN", 1))
    max_k = max(min_k, getattr(settings, "SM_RAG_TOPK_MAX", min_k))
    default_k = getattr(settings, "SM_RAG_TOPK", min_k)
    try:
        normalized = int(value) if value is not None else default_k
    except (TypeError, ValueError):
        normalized = default_k
    return max(min_k, min(max_k, normalized))
