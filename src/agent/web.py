"""Web acquisition: Tavily search, resilient content fetching, markdown conversion.

Fetching sits behind `ContentFetcher`: direct httpx + trafilatura is primary,
Tavily Extract is the per-page fallback for bot-protected or JS-rendered sites
(spec §7). Search returns metadata only — acquisition is a separate stage.
"""

import asyncio
import re
from dataclasses import dataclass

import httpx
import trafilatura
from tavily import AsyncTavilyClient
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from agent.config import Settings

# ZWSP, ZWNJ, ZWJ, word joiner, BOM — escaped so the invisibles are visible in source
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/=]{100,}")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_UA = "Mozilla/5.0 (compatible; memory-first-agent/0.1; research prototype)"


def strip_structural(md: str) -> str:
    """Layer-2 structural sanitation (spec §8): remove carriers of hidden content."""
    md = _ZERO_WIDTH.sub("", md)
    md = _HTML_COMMENT.sub("", md)
    md = _BASE64_BLOB.sub("[data removed]", md)
    return md.strip()


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


@dataclass
class PageContent:
    url: str
    title: str
    markdown: str


class SearchClient:
    def __init__(self, settings: Settings, tavily: AsyncTavilyClient | None = None):
        self._settings = settings
        self._tavily = tavily or AsyncTavilyClient(api_key=settings.tavily_api_key)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.5, max=4), reraise=True
    )
    async def search(self, query: str) -> list[SearchResult]:
        response = await asyncio.wait_for(
            self._tavily.search(query, max_results=self._settings.top_k, search_depth="basic"),
            timeout=self._settings.search_timeout_s,
        )
        return [
            SearchResult(url=r["url"], title=r.get("title", ""), snippet=r.get("content", ""))
            for r in response.get("results", [])
        ]


class ContentFetcher:
    def __init__(self, settings: Settings, tavily: AsyncTavilyClient | None = None):
        self._settings = settings
        self._tavily = tavily or AsyncTavilyClient(api_key=settings.tavily_api_key)
        self._http = httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": _UA},
            timeout=settings.fetch_timeout_s,
        )

    async def _fetch_direct(self, url: str) -> PageContent | None:
        response = await self._http.get(url)
        response.raise_for_status()
        html = response.text
        markdown = trafilatura.extract(html, output_format="markdown", include_comments=False)
        if not markdown or not markdown.strip():
            return None
        title_match = _TITLE.search(html)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        return PageContent(url=url, title=title, markdown=markdown)

    async def _fetch_via_extract(self, url: str) -> PageContent | None:
        response = await asyncio.wait_for(
            self._tavily.extract(urls=[url], format="markdown"),
            timeout=self._settings.fetch_timeout_s,
        )
        results = response.get("results", [])
        if not results or not results[0].get("raw_content"):
            return None
        return PageContent(
            url=url, title=results[0].get("title", ""), markdown=results[0]["raw_content"]
        )

    async def fetch(self, url: str) -> PageContent | None:
        try:
            page = await self._fetch_direct(url)
            if page is not None:
                return page
        except Exception:
            pass  # fall through to the extract fallback; per-page failures never fail the turn
        try:
            return await self._fetch_via_extract(url)
        except Exception:
            return None

    async def fetch_all(self, urls: list[str]) -> list[PageContent]:
        pages = await asyncio.gather(*(self.fetch(u) for u in urls))
        return [p for p in pages if p is not None]

    async def aclose(self) -> None:
        await self._http.aclose()
