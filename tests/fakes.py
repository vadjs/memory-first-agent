"""Shared fakes for the seams both suites mimic: MemoryStore, UtilityLLM,
ContentFetcher. One mimic per real seam — when a real signature changes, this is
the single place the fakes must follow."""

from agent.guardrails import PreflightOut, ScreenOut
from agent.memory import CacheHit, ChunkHit
from agent.summarizer import SummaryOut
from agent.telemetry import Usage
from agent.web import PageContent


class FakeUtil:
    def __init__(self, pf: PreflightOut | None = None, verdicts=None, summary="A digest."):
        self.pf = pf
        self.verdicts = verdicts
        self.summary = summary
        self.summary_users: list[str] = []

    async def complete_json(self, system, user, schema):
        usage = Usage("gpt-5-nano", 100, 20)
        if schema is PreflightOut:
            return self.pf, usage
        if schema is SummaryOut:
            self.summary_users.append(user)
            return SummaryOut(summary=self.summary), usage
        n = user.count("--- BLOCK")
        verdicts = self.verdicts if self.verdicts is not None else ["content"] * n
        return ScreenOut(verdicts=verdicts[:n] + ["content"] * max(0, n - len(verdicts))), usage


class FakeMemory:
    def __init__(self, cache: CacheHit | None = None, chunks: list[ChunkHit] | None = None):
        self.cache = cache
        self.chunks = chunks or []
        self.qa_writes: list[tuple] = []
        self.upserted: list = []
        self.marked: list[str] = []
        self.recent_urls: set[str] = set()
        self.stored_by_url: dict[str, list[ChunkHit]] = {}

    async def search_cache(self, query):
        return self.cache, None

    async def search_chunks(self, query, k):
        return self.chunks, None

    async def put_qa(self, question, answer, urls, topic, temporal):
        self.qa_writes.append((question, answer, urls, topic, temporal))

    async def upsert_chunks(self, records):
        self.upserted.extend(records)
        return len(records), None

    async def mark_url_ingested(self, url, ttl_days):
        self.marked.append(url)

    async def url_recently_ingested(self, url):
        return url in self.recent_urls

    async def chunks_for_url(self, url):
        return list(self.stored_by_url.get(url, []))


class FakeFetcher:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.calls: list[list[str]] = []

    async def fetch_all(self, urls):
        self.calls.append(list(urls))
        return self.pages


PAGE = PageContent(
    url="https://web.test/article",
    title="Article",
    markdown="# Patterns\n\nThe strangler fig pattern replaces legacy systems via a facade.",
)
