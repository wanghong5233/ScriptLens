"""Prompt loader utilities for Doc Studio YAML bundles."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml


@lru_cache(maxsize=16)
def load_prompt_bundle(module: str, language: str) -> Dict[str, Any]:
    """Load a YAML prompt bundle for a module.

    Args:
        module (str): Module name (e.g., "doc_studio").
        language (str): Language code (e.g., "zh", "en").

    Returns:
        Dict[str, Any]: Prompt bundle dictionary.
    """

    base_dir = Path(__file__).resolve().parents[1] / "prompts" / module
    candidate = base_dir / f"{language}.yaml"
    fallback = base_dir / "en.yaml"
    path = candidate if candidate.exists() else fallback
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def clear_prompt_cache() -> None:
    """Clear cached prompt bundles."""

    load_prompt_bundle.cache_clear()
