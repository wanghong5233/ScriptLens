"""Web search client for Doc Studio."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class WebSearchClient:
    """Async client for web search providers."""

    def __init__(
        self,
        provider: str,
        api_key: Optional[str],
        base_url: Optional[str],
        timeout: int = 20,
    ) -> None:
        """Initialize the web search client.

        Args:
            provider (str): Provider name (e.g., tavily, serper).
            api_key (Optional[str]): API key for the provider.
            base_url (Optional[str]): Override base URL.
            timeout (int): Request timeout in seconds.
        """

        self._provider = (provider or "tavily").lower()
        self._api_key = api_key
        self._base_url = base_url
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        await self._client.aclose()

    def is_configured(self) -> bool:
        """Check if the client has required credentials."""

        return bool(self._api_key)

    async def search(self, query: str, max_results: int = 5) -> Dict[str, Any]:
        """Execute a web search and return normalized results."""

        if self._provider == "tavily":
            return await self._search_tavily(query, max_results)
        if self._provider == "serper":
            return await self._search_serper(query, max_results)
        raise ValueError(f"Unsupported web search provider: {self._provider}")

    async def _search_tavily(self, query: str, max_results: int) -> Dict[str, Any]:
        """Call Tavily search API."""

        url = self._base_url or "https://api.tavily.com/search"
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": False,
        }
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        results = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content") or item.get("snippet"),
            }
            for item in data.get("results", [])
        ]
        return {"provider": "tavily", "query": query, "results": results, "raw": data}

    async def _search_serper(self, query: str, max_results: int) -> Dict[str, Any]:
        """Call Serper search API."""

        url = self._base_url or "https://google.serper.dev/search"
        headers = {"X-API-KEY": self._api_key or ""}
        payload = {"q": query, "num": max_results}
        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        results = [
            {
                "title": item.get("title"),
                "url": item.get("link"),
                "snippet": item.get("snippet"),
            }
            for item in data.get("organic", [])
        ]
        return {"provider": "serper", "query": query, "results": results, "raw": data}


_web_search_client: Optional[WebSearchClient] = None


def get_web_search_client(provider: str, api_key: Optional[str], base_url: Optional[str], timeout: int) -> WebSearchClient:
    """Get a cached web search client instance."""

    global _web_search_client
    if _web_search_client is None:
        _web_search_client = WebSearchClient(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
    return _web_search_client
