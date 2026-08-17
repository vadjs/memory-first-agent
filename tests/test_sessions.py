"""The Session rule (CONTEXT.md: last 10 turns) through both adapters."""

import pytest

from agent.sessions import DEFAULT_MAX_TURNS, InMemorySessionStore, SessionStore


async def test_in_memory_trims_to_ten_turns():
    store = InMemorySessionStore()
    for i in range(DEFAULT_MAX_TURNS + 2):
        await store.append("s", f"q{i}", f"a{i}")
    history = await store.get("s")
    assert len(history) == DEFAULT_MAX_TURNS * 2
    assert history[0] == {"role": "user", "content": "q2"}  # oldest two turns dropped
    assert history[-1] == {"role": "assistant", "content": f"a{DEFAULT_MAX_TURNS + 1}"}


async def test_in_memory_sessions_are_isolated():
    store = InMemorySessionStore()
    await store.append("a", "q", "ans")
    assert await store.get("b") == []
    (await store.get("a")).clear()  # mutating the returned list must not leak
    assert len(await store.get("a")) == 2


@pytest.mark.redis
async def test_redis_adapter_same_rule_and_ttl():
    from redis.asyncio import Redis

    r = Redis.from_url("redis://localhost:6379")
    try:
        await r.ping()
    except Exception:
        pytest.skip("redis not running")
    store = SessionStore(r, namespace="mfatest")
    try:
        for i in range(DEFAULT_MAX_TURNS + 2):
            await store.append("s", f"q{i}", f"a{i}")
        history = await store.get("s")
        assert len(history) == DEFAULT_MAX_TURNS * 2
        assert history[0] == {"role": "user", "content": "q2"}
        ttl = await r.ttl(store._key("s"))
        assert 0 < ttl <= store.ttl_s
    finally:
        async for k in r.scan_iter(match="mfatest:session:*"):
            await r.delete(k)
        await r.aclose()
