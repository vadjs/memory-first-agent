import time

import pytest

from agent.embeddings import FakeEmbedder
from agent.memory import (
    MemoryStore,
    normalize_question,
    normalize_text,
    normalize_url,
    sha,
    to_similarity,
)

# -- unit: helpers, no redis --------------------------------------------------


def test_normalize_url_strips_tracking_and_fragment():
    assert (
        normalize_url("HTTPS://Example.com/Docs/?utm_source=x&q=1#top")
        == "https://example.com/Docs?q=1"
    )


def test_normalize_url_equivalence():
    assert normalize_url("https://a.com/p/") == normalize_url("https://A.com/p")


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  a \n\n b\t c ") == "a b c"


def test_question_normalization_is_case_insensitive():
    assert sha(normalize_question("What Is Redis?")) == sha(normalize_question("what is redis?"))


def test_to_similarity():
    assert to_similarity(0.0) == 1.0
    assert to_similarity(0.3) == pytest.approx(0.7)


async def test_fake_embedder_deterministic():
    e = FakeEmbedder(dim=8)
    [a], [b] = await e.embed(["same text"]), await e.embed(["same text"])
    assert a == b
    [c] = await e.embed(["other text"])
    assert a != c


async def test_fake_embedder_pinned_vectors_normalized():
    e = FakeEmbedder(dim=2, vectors={"x": [3.0, 4.0]})
    [v] = await e.embed(["x"])
    assert v == pytest.approx([0.6, 0.8])


# -- integration: real Redis 8 ------------------------------------------------

pytestmark_redis = pytest.mark.redis


@pytest.fixture
async def store():
    s = MemoryStore("redis://localhost:6379", FakeEmbedder(dim=8), namespace="mfatest")
    try:
        await s.ping()
    except Exception:
        pytest.skip("redis not running")
    await s.clear()
    await s.ensure_indexes()
    yield s
    await s.clear()
    await s.aclose()


CHUNK = {
    "text": "Redis 8 bundles the query engine with vector search.",
    "url": "https://redis.io/docs",
    "title": "Redis docs",
    "section": "Vectors",
}


@pytest.mark.redis
async def test_indexes_idempotent(store):
    await store.ensure_indexes()  # second call must not raise


@pytest.mark.redis
async def test_upsert_dedup(store):
    await store.upsert_chunks([CHUNK])
    await store.upsert_chunks([{**CHUNK, "title": "changed"}])  # same text → same key
    stats = await store.stats()
    assert stats["chunks"] == 1


@pytest.mark.redis
async def test_search_identical_text_is_similarity_one(store):
    await store.upsert_chunks([CHUNK])
    hits = await store.search_chunks(CHUNK["text"], k=3)
    assert hits and hits[0].similarity > 0.99
    assert hits[0].url == "https://redis.io/docs"


@pytest.mark.redis
async def test_quarantined_chunks_invisible_to_retrieval(store):
    await store.upsert_chunks([{**CHUNK, "quarantined": True}])
    stats = await store.stats()
    assert stats["chunks"] == 1 and stats["quarantined"] == 1
    assert await store.search_chunks(CHUNK["text"], k=3) == []


@pytest.mark.redis
async def test_answer_cache_roundtrip_and_paraphrase_gap(store):
    await store.put_qa("What is Redis?", "A data store.", ["https://redis.io"], "technology", "static")
    hit = await store.search_cache("what is redis?")  # case-insensitive normalization
    assert hit is not None and hit.similarity > 0.99
    assert hit.answer == "A data store."
    other = await store.search_cache("how do plants grow")
    assert other is None or other.similarity < 0.9


@pytest.mark.redis
async def test_forget_url_cascades_both_tiers(store):
    await store.upsert_chunks([CHUNK])
    await store.put_qa("What is Redis?", "A data store.", ["https://redis.io/docs"], "technology", "static")
    await store.mark_url_ingested("https://redis.io/docs", ttl_days=7)
    deleted = await store.forget_url("https://redis.io/docs/")  # trailing slash normalizes away
    assert deleted >= 3
    stats = await store.stats()
    assert stats["chunks"] == 0 and stats["qa"] == 0


@pytest.mark.redis
async def test_forget_question(store):
    await store.put_qa("What is Redis?", "A data store.", [], "technology", "static")
    assert await store.forget_question("what is REDIS?") == 1
    assert (await store.stats())["qa"] == 0


@pytest.mark.redis
async def test_cleanup_removes_only_old(store):
    await store.upsert_chunks([CHUNK])
    old_key = store.chunk_prefix + "0" * 64
    await store.r.hset(old_key, mapping={"text": "old", "fetched_at": time.time() - 90 * 86400})
    assert await store.cleanup(older_than_days=30) == 1
    assert (await store.stats())["chunks"] == 1


@pytest.mark.redis
async def test_url_marker_ttl(store):
    await store.mark_url_ingested("https://a.com/x", ttl_days=1)
    assert await store.url_recently_ingested("https://a.com/x") is True
    assert await store.url_recently_ingested("https://a.com/other") is False
    ttl = await store.r.ttl(store.url_prefix + sha(normalize_url("https://a.com/x")))
    assert 0 < ttl <= 86400
