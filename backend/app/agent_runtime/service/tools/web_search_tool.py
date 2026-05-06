"""Web search tool for Doc Studio."""

from __future__ import annotations

from typing import Any, Dict

from ...core.config import settings
from ..web_search_client import get_web_search_client
from .base_tool import BaseTool, ToolResult


class WebSearchTool(BaseTool):
    """Search the web for up-to-date information."""

    def __init__(self) -> None:
        super().__init__(
            name="web_search_tool",
            description="Web search for latest information and sources.",
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results",
                    "default": settings.WEB_SEARCH_MAX_RESULTS,
                },
            },
            "required": ["query"],
        }

    async def execute(self, _agent_state: Any, parameters: Dict[str, Any]) -> ToolResult:
        query = (parameters or {}).get("query") or ""
        query = str(query).strip()
        if not query:
            return ToolResult(success=False, error="query is required", summary="Missing search query")

        client = get_web_search_client(
            provider=settings.WEB_SEARCH_PROVIDER,
            api_key=settings.WEB_SEARCH_API_KEY,
            base_url=settings.WEB_SEARCH_BASE_URL,
            timeout=settings.WEB_SEARCH_TIMEOUT,
        )
        if not client.is_configured():
            return ToolResult(
                success=True,
                data={"skipped": True, "reason": "missing_api_key"},
                summary="Web search API key not configured",
            )

        max_results = parameters.get("max_results") or settings.WEB_SEARCH_MAX_RESULTS
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = settings.WEB_SEARCH_MAX_RESULTS

        try:
            payload = await client.search(query=query, max_results=max_results)
            results = payload.get("results") or []
            return ToolResult(
                success=True,
                data=payload,
                summary=f"Found {len(results)} web results",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                summary="Web search failed",
            )
