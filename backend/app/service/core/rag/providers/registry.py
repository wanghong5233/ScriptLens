"""Registry helpers for RAG providers."""

from __future__ import annotations

from typing import Optional

from core.config import settings


def normalize_provider(provider: Optional[str]) -> Optional[str]:
    """Normalize provider name."""
    if not provider:
        return None
    return provider.strip().lower()


def get_allowed_providers() -> set[str]:
    """Return allowed provider names."""
    raw = getattr(settings, "SM_RAG_PROVIDER_ALLOWLIST", "") or ""
    items = [seg.strip().lower() for seg in raw.split(",") if seg.strip()]
    return set(items) or {"multi_stage"}


def resolve_provider(requested: Optional[str], fallback: Optional[str] = None) -> str:
    """Resolve provider name with allowlist and fallback."""
    alias_map = {
        "lightrag": "graph",
        "raganything": "multimodal_graph",
        "academic": "graph",
        "llamaindex": "multi_stage",
    }
    allowed = get_allowed_providers()
    candidate = normalize_provider(requested) or normalize_provider(fallback)
    if candidate in alias_map:
        candidate = alias_map[candidate]
    if candidate and candidate in allowed:
        return candidate
    default_provider = normalize_provider(getattr(settings, "SM_DEFAULT_RAG_PROVIDER", None))
    if default_provider in alias_map:
        default_provider = alias_map[default_provider]
    if default_provider and default_provider in allowed:
        return default_provider
    return sorted(allowed)[0]
