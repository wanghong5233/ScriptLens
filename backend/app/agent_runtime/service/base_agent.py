"""
Base agent utilities for shared capabilities.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..utils.language import guess_language
from ..utils.prompt_loader import load_prompt_bundle


class BaseAgent:
    """Base class for agent implementations."""

    def __init__(self, llm_client: Any, tool_registry: Any, agent_name: str, prompt_module: str) -> None:
        self.llm = llm_client
        self.tools = tool_registry
        self.agent_name = agent_name
        self.prompt_module = prompt_module

    def get_prompt(self, key: str, language: Optional[str] = None, fallback: str = "") -> str:
        """Fetch a prompt template by key.

        Args:
            key (str): Prompt key in YAML.
            language (Optional[str]): Language code.
            fallback (str): Fallback when missing.

        Returns:
            str: Prompt template string.
        """

        lang = language or "en"
        bundle = load_prompt_bundle(self.prompt_module, lang)
        value = bundle.get(key) or ""
        if not value and lang != "en":
            bundle = load_prompt_bundle(self.prompt_module, "en")
            value = bundle.get(key) or ""
        return (value or fallback).strip()

    def infer_language(self, text: str) -> str:
        """Infer language from text."""

        return guess_language(text or "")

    def refresh_config(self) -> Dict[str, Any]:
        """Refresh config via the underlying LLM client."""

        if hasattr(self.llm, "refresh_config"):
            return self.llm.refresh_config()
        return {"status": "skipped"}
