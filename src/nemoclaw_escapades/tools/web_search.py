"""Web search and URL fetch tools for the orchestrator.

Provides two tools:

- ``web_search`` — query the Brave Search API and return a summary of
  results (title, URL, snippet).
- ``web_fetch`` — fetch a URL via the Jina Reader API
  (``https://r.jina.ai/``) and return clean Markdown.  Falls back to
  a direct fetch with HTML stripping if Jina is unavailable or
  rate-limited.

**Auth:**

- **Brave Search** requires an API key via ``X-Subscription-Token``.
  Set ``BRAVE_SEARCH_API_KEY``.
- **Jina Reader** uses the free tier by default — no API key needed,
  rate-limited to 20 RPM.  This is sufficient for typical agent
  workloads.  To increase the limit to 500 RPM, set ``JINA_API_KEY``
  (free keys come with 10M tokens; see https://jina.ai/reader/).

Both Hermes and OpenClaw expose equivalent ``web_search`` /
``web_fetch`` tools.  This implementation follows the same contract.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any

import httpx

from nemoclaw_escapades.config import WebSearchConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.web_search")

# ── Constants ─────────────────────────────────────────────────────────

# Brave Search API endpoint
_BRAVE_SEARCH_URL: str = "https://api.search.brave.com/res/v1/web/search"
# Jina Reader API prefix — prepend to any URL to get Markdown
_JINA_READER_PREFIX: str = "https://r.jina.ai/"
# Seconds before an HTTP request is aborted
_REQUEST_TIMEOUT_S: float = 30.0
# Max characters of response body included in error messages
_ERROR_BODY_MAX_CHARS: int = 500
# Default max search results returned
_DEFAULT_SEARCH_LIMIT: int = 5
# Max characters returned from a fetched web page
_FETCH_MAX_CHARS: int = 32_768
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "web_search"


# ── Async Brave Search client ────────────────────────────────────────


class BraveSearchClient:
    """Async client for the Brave Search API.

    Attributes:
        configured: Whether the API key is present.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Return ``True`` when the API key is set."""
        return bool(self._api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first call."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
                timeout=_REQUEST_TIMEOUT_S,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, count: int = _DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        """Search the web via Brave Search API.

        Args:
            query: Search query string.
            count: Number of results to return.

        Returns:
            Parsed JSON response from Brave, or an error dict.
        """
        if not self.configured:
            return {"error": "Web search not configured. Set BRAVE_SEARCH_API_KEY."}
        client = await self._get_client()
        response = await client.get(
            _BRAVE_SEARCH_URL,
            params={"q": query, "count": count},
        )
        if response.status_code >= 400:
            return {
                "error": f"Brave Search API returned {response.status_code}",
                "body": response.text[:_ERROR_BODY_MAX_CHARS],
            }
        return response.json()  # type: ignore[no-any-return]


# ── Helpers ───────────────────────────────────────────────────────────


def _format(data: Any) -> str:
    """Serialize *data* as indented JSON for model consumption."""
    return json.dumps(data, indent=2, default=str)


def _format_search_results(data: dict[str, Any]) -> str:
    """Extract and format web results into a concise summary.

    Args:
        data: Raw Brave Search API response.

    Returns:
        Numbered list of results with title, URL, and snippet, or
        a JSON error string if the response has no web results.
    """
    if "error" in data:
        return _format(data)

    results = data.get("web", {}).get("results", [])
    if not results:
        return "No results found."

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("url", "")
        snippet = r.get("description", "")
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


# ── Tool specs ────────────────────────────────────────────────────────


def _make_web_search(
    client: BraveSearchClient,
    default_limit: int = _DEFAULT_SEARCH_LIMIT,
) -> ToolSpec:
    """Create the ``web_search`` tool spec.

    Args:
        client: Brave Search API client.
        default_limit: Default number of results when the model omits ``count``.
    """

    @tool(
        "web_search",
        "Search the web for real-time information. Returns titles, URLs, and snippets for the top results. Use for current events, documentation lookups, or any question that needs up-to-date information.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "count": {
                    "type": "integer",
                    "description": "Number of results to return.",
                    "default": default_limit,
                },
            },
            "required": ["query"],
        },
        display_name="Searching the web",
        toolset=_TOOLSET,
    )
    async def web_search(query: str, count: int = default_limit) -> str:
        """Search the web via Brave Search and return formatted results.

        Args:
            query: Search query string.
            count: Number of results to return.

        Returns:
            Numbered list of results with title, URL, and snippet.
        """
        data = await client.search(query, count=count)
        return _format_search_results(data)

    return web_search


async def _fetch_via_jina(url: str, jina_api_key: str) -> str | None:
    """Try fetching *url* through Jina Reader, returning Markdown or ``None``.

    Returns ``None`` on any failure (network error, rate limit, 4xx/5xx)
    so the caller can fall back to a direct fetch.

    Args:
        url: Target URL.
        jina_api_key: Jina API key (empty string for anonymous access).

    Returns:
        Markdown string on success, or ``None`` on failure.
    """
    headers: dict[str, str] = {"Accept": "text/markdown"}
    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"

    reader_url = f"{_JINA_READER_PREFIX}{url}"
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            response = await client.get(reader_url, headers=headers)
    except httpx.HTTPError:
        return None

    if response.status_code >= 400:
        return None
    return response.text


async def _fetch_direct(url: str) -> str:
    """Fetch *url* directly and strip HTML to plain text.

    Used as a fallback when Jina Reader is unavailable or rate-limited.

    Args:
        url: Target URL.

    Returns:
        Plain-text content, or an error message.
    """
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "NemoClaw-Agent/1.0"},
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return f"Error fetching {url}: {exc}"

    if response.status_code >= 400:
        return f"Error: HTTP {response.status_code} fetching {url}"

    text = response.text
    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        text = _strip_html(text)
    return text


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities, returning plain text.

    Args:
        text: Raw HTML string.

    Returns:
        Cleaned plain text with collapsed whitespace.
    """
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _make_web_fetch(jina_api_key: str = "") -> ToolSpec:
    """Create the ``web_fetch`` tool spec.

    Tries the Jina Reader API first for clean Markdown output.  Falls
    back to a direct fetch with HTML stripping if Jina is unavailable
    or rate-limited.
    """

    @tool(
        "web_fetch",
        "Fetch a web page by URL and return its content as Markdown. Use after web_search to read a specific page, or to fetch documentation, README files, API references, etc.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (https://...)."},
            },
            "required": ["url"],
        },
        display_name="Fetching web page",
        toolset=_TOOLSET,
    )
    async def web_fetch(url: str) -> str:
        """Fetch a URL, preferring Jina Reader for Markdown, with direct fallback.

        Args:
            url: Full URL to fetch.

        Returns:
            Page content (Markdown from Jina, or plain text from direct
            fetch), truncated to ``_FETCH_MAX_CHARS``.
        """
        text = await _fetch_via_jina(url, jina_api_key)
        if text is None:
            logger.info("Jina Reader unavailable, falling back to direct fetch", extra={"url": url})
            text = await _fetch_direct(url)

        if len(text) > _FETCH_MAX_CHARS:
            text = text[:_FETCH_MAX_CHARS] + f"\n... (truncated at {_FETCH_MAX_CHARS} chars)"
        return text

    return web_fetch


# ── Registration ──────────────────────────────────────────────────────


def register_web_search_tools(registry: ToolRegistry, config: WebSearchConfig) -> None:
    """Register web_search and web_fetch tools with the orchestrator registry.

    ``web_search`` requires a Brave API key; ``web_fetch`` works without
    one (it fetches URLs directly).  Both are registered together — if
    the API key is missing, ``web_search`` will return an error at call
    time but ``web_fetch`` will still work.

    Args:
        registry: The tool registry to populate.
        config: Web search configuration (API key, default limit).
    """
    client = BraveSearchClient(api_key=config.api_key)

    def _check() -> bool:
        return client.configured

    search_spec = _make_web_search(client, default_limit=config.default_limit)
    search_spec.check_fn = _check
    registry.register(search_spec)

    registry.register(_make_web_fetch(jina_api_key=config.jina_api_key))
