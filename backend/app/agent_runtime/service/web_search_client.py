"""Web search client for Doc Studio."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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

    async def search(
        self,
        query: str,
        max_results: int = 5,
        *,
        search_depth: str = "basic",
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Execute a web search and return normalized results.

        Args:
            query: Free-text search query.
            max_results: Upper bound of results returned by provider.
            search_depth: Tavily-specific. "basic" (fast) or "advanced"
                (deeper crawl, slower, higher relevance). Ignored by serper.
            include_domains: Whitelist hostnames (e.g. ["douyin.com"]).
                Tavily uses native `include_domains`; serper uses
                Google `site:` operator joined with OR.
            exclude_domains: Blacklist hostnames. Same routing as include.
        """

        if self._provider == "tavily":
            return await self._search_tavily(
                query,
                max_results,
                search_depth=search_depth,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
        if self._provider == "serper":
            return await self._search_serper(
                query,
                max_results,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
        raise ValueError(f"Unsupported web search provider: {self._provider}")

    async def _search_tavily(
        self,
        query: str,
        max_results: int,
        *,
        search_depth: str = "basic",
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Call Tavily search API.

        Tavily payload reference (v0 docs, 2026-05):
          - search_depth: "basic" | "advanced"
          - include_domains: List[str]
          - exclude_domains: List[str]
          - include_answer / include_raw_content: bool
        """

        url = self._base_url or "https://api.tavily.com/search"
        payload: Dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": False,
        }
        if search_depth in {"basic", "advanced"}:
            payload["search_depth"] = search_depth
        if include_domains:
            payload["include_domains"] = list(include_domains)
        if exclude_domains:
            payload["exclude_domains"] = list(exclude_domains)
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        results = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content") or item.get("snippet"),
                "score": item.get("score"),
            }
            for item in data.get("results", [])
        ]
        return {"provider": "tavily", "query": query, "results": results, "raw": data}

    async def _search_serper(
        self,
        query: str,
        max_results: int,
        *,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Call Serper search API.

        Serper doesn't have native include/exclude domain — we splice Google
        `site:` operators into the query as a best-effort polyfill.
        """

        url = self._base_url or "https://google.serper.dev/search"
        headers = {"X-API-KEY": self._api_key or ""}
        domain_clauses: List[str] = []
        if include_domains:
            inc = " OR ".join(f"site:{d}" for d in include_domains)
            domain_clauses.append(f"({inc})")
        if exclude_domains:
            domain_clauses.extend(f"-site:{d}" for d in exclude_domains)
        final_query = query if not domain_clauses else f"{query} {' '.join(domain_clauses)}"
        payload = {"q": final_query, "num": max_results}
        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        results = [
            {
                "title": item.get("title"),
                "url": item.get("link"),
                "snippet": item.get("snippet"),
                "score": None,
            }
            for item in data.get("organic", [])
        ]
        return {"provider": "serper", "query": final_query, "results": results, "raw": data}


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
