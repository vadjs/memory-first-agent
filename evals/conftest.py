"""Shared eval harness: real Redis vector memory + pinned deterministic embeddings,
fake web and LLMs — so routing decisions under test are the real similarity math."""

import math
import time

import pytest

from agent.config import Settings
from agent.embeddings import FakeEmbedder
from agent.guardrails import PreflightOut, ScreenOut
from agent.memory import MemoryStore
from agent.pipeline import Pipeline
from agent.telemetry import Usage
from agent.web import PageContent, SearchResult

QUERY = "what is the strangler fig pattern"
SEEDED_QUESTION = "explain the strangler fig approach for legacy systems"
SEEDED_CHUNK = "The strangler fig pattern replaces legacy systems incrementally via a facade."
PAGE_URL = "https://web.test/article"
KB_URL = "https://kb.test/a"


def vec_at(cosine: float) -> list[float]:
    """A unit vector at the given cosine similarity to the base direction e1."""
    angle = math.acos(max(-1.0, min(1.0, cosine)))
    return [math.cos(angle), math.sin(angle)] + [0.0] * 6


def pinned_embedder(chunk_cos: float = 1.0, cache_cos: float = 1.0) -> FakeEmbedder:
    """The query embeds at e1; seeded texts embed at the requested cosines to it."""
    return FakeEmbedder(
        dim=8,
        vectors={
            QUERY: vec_at(1.0),
            SEEDED_QUESTION: vec_at(cache_cos),
            SEEDED_CHUNK: vec_at(chunk_cos),
        },
    )


class EvalUtil:
    """Preflight controlled by the eval case; screening flags 'ignore previous'."""

    def __init__(self, temporal="static", contains_pii=False):
        self.temporal = temporal
        self.contains_pii = contains_pii

    async def complete_json(self, system, user, schema):
        usage = Usage("gpt-5-nano", 50, 10)
        if schema is PreflightOut:
            return (
                PreflightOut(
                    is_injection=False,
                    temporal=self.temporal,
                    topic="technology",
                    contains_pii=self.contains_pii,
                    standalone_query=QUERY,
                ),
                usage,
            )
        blocks = user.split("--- BLOCK")[1:]
        verdicts = [
            "instruction_like" if "previous instructions" in b.lower() else "content"
            for b in blocks
        ]
        return ScreenOut(verdicts=verdicts), usage


class EvalConv:
    def __init__(self, answer=f"Grounded answer. Sources: {PAGE_URL}"):
        self.answer = answer
        self.calls: list[str] = []

    async def synthesize(self, user_message):
        self.calls.append(user_message)
        return self.answer, Usage("gpt-5.6-luna", 2000, 200)


class EvalSearch:
    def __init__(self, down=False):
        self.down = down

    async def search(self, query):
        if self.down:
            raise RuntimeError("search unavailable")
        return [SearchResult(url=PAGE_URL, title="Article", snippet="s")]


class EvalFetcher:
    def __init__(self, markdown="# Patterns\n\nThe strangler fig pattern uses a facade."):
        self.markdown = markdown

    async def fetch_all(self, urls):
        if not urls:
            return []
        return [PageContent(url=PAGE_URL, title="Article", markdown=self.markdown)]


@pytest.fixture
async def eval_env():
    """Yields a builder: seed real Redis memory per case, get a wired pipeline back."""
    stores: list[MemoryStore] = []

    async def build(
        seed: str = "none",
        temporal: str = "static",
        chunk_cos: float = 1.0,
        cache_cos: float = 1.0,
        search_down: bool = False,
        conv: EvalConv | None = None,
        contains_pii: bool = False,
        page_markdown: str | None = None,
    ):
        embedder = pinned_embedder(chunk_cos=chunk_cos, cache_cos=cache_cos)
        store = MemoryStore("redis://localhost:6379", embedder, namespace="mfaeval")
        try:
            await store.ping()
        except Exception:
            pytest.skip("redis not running")
        stores.append(store)
        await store.clear()
        await store.ensure_indexes()

        age = None
        if seed.startswith("cache"):
            await store.put_qa(SEEDED_QUESTION, "Cached answer", [KB_URL], "technology", temporal)
        elif seed.startswith("chunks"):
            await store.upsert_chunks(
                [{"text": SEEDED_CHUNK, "url": KB_URL, "title": "KB", "section": "Patterns"}]
            )
            if seed == "chunks_stale":
                age = time.time() - 30 * 86400
        if age is not None:
            async for key in store.r.scan_iter(match=f"{store.chunk_prefix}*"):
                await store.r.hset(key, "fetched_at", age)

        fetcher = EvalFetcher(page_markdown) if page_markdown else EvalFetcher()
        pipeline = Pipeline(
            Settings(_env_file=None),
            store,
            EvalSearch(down=search_down),
            fetcher,
            conv or EvalConv(),
            EvalUtil(temporal=temporal, contains_pii=contains_pii),
        )
        return pipeline, store

    yield build
    for s in stores:
        await s.clear()
        await s.aclose()


@pytest.fixture(autouse=True)
def _log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path))
