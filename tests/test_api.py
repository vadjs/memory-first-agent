import httpx
import pytest

from agent.api import create_app
from agent.config import Settings
from agent.domain import QueryTooLongError, Route, Temporal
from agent.pipeline import TurnResult
from agent.ratelimit import TokenBucket
from agent.telemetry import TurnRecord


def record(turn_id="t-1", route=Route.MISS_WEB) -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        query="q",
        route=route,
        topic="technology",
        temporal=Temporal.STATIC,
        injection_flagged=False,
        contains_pii=False,
        scores={},
        stages=[],
        usages=[],
        total_cost_usd=0.001,
        cited_urls=["https://a.com"],
    )


class FakeMemoryHandle:
    def __init__(self, healthy=True):
        self.healthy = healthy

    async def ensure_indexes(self):
        pass

    async def ping(self):
        if not self.healthy:
            raise ConnectionError("down")
        return True


class FakePipeline:
    def __init__(self, healthy=True, max_query_chars: int | None = None):
        self.memory = FakeMemoryHandle(healthy)
        self.history_seen: list[list[dict]] = []
        self.max_query_chars = max_query_chars

    async def answer_turn(self, query, history=None, session_id=""):
        # Mimic the Pipeline's input invariant (its interface, not just its type).
        if self.max_query_chars is not None and len(query) > self.max_query_chars:
            raise QueryTooLongError(f"query exceeds {self.max_query_chars} characters")
        self.history_seen.append(list(history or []))
        return TurnResult("An answer", Route.MISS_WEB, [{"url": "https://a.com"}], record())


class FakeSessions:
    def __init__(self):
        self.data: dict[str, list[dict]] = {}

    async def get(self, session_id):
        return self.data.get(session_id, [])

    async def append(self, session_id, user, assistant):
        self.data.setdefault(session_id, []).extend(
            [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
        )


class FakeLimiter:
    def __init__(self, allowed=True):
        self.allowed = allowed

    async def allow(self, key):
        return self.allowed


def client(pipeline=None, settings=None, limiter=None, sessions=None):
    api = create_app(
        pipeline or FakePipeline(),
        sessions or FakeSessions(),
        settings or Settings(_env_file=None, api_key="secret"),
        limiter or FakeLimiter(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


AUTH = {"Authorization": "Bearer secret"}


async def test_chat_answers_and_creates_session():
    async with client() as c:
        r = await c.post("/chat", json={"message": "hello"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "An answer" and body["route"] == "miss_web"
    assert body["session_id"]


async def test_session_history_flows_into_next_turn():
    pipeline = FakePipeline()
    sessions = FakeSessions()
    async with client(pipeline=pipeline, sessions=sessions) as c:
        first = await c.post("/chat", json={"message": "first"}, headers=AUTH)
        sid = first.json()["session_id"]
        await c.post("/chat", json={"message": "second", "session_id": sid}, headers=AUTH)
    assert pipeline.history_seen[0] == []
    assert pipeline.history_seen[1][0]["content"] == "first"


async def test_missing_or_wrong_bearer_is_401():
    async with client() as c:
        assert (await c.post("/chat", json={"message": "x"})).status_code == 401
        bad = {"Authorization": "Bearer wrong"}
        assert (await c.post("/chat", json={"message": "x"}, headers=bad)).status_code == 401


async def test_rate_limit_429():
    async with client(limiter=FakeLimiter(allowed=False)) as c:
        r = await c.post("/chat", json={"message": "x"}, headers=AUTH)
    assert r.status_code == 429


async def test_message_over_cap_is_422():
    # The Pipeline raises QueryTooLongError; the HTTP adapter only translates it to 422.
    async with client(pipeline=FakePipeline(max_query_chars=10)) as c:
        r = await c.post("/chat", json={"message": "x" * 11}, headers=AUTH)
    assert r.status_code == 422
    assert "exceeds 10 characters" in r.json()["detail"]


async def test_internal_valueerror_is_not_repainted_as_422():
    """Only the typed input invariant becomes a 422; any other ValueError from the
    turn (corrupt cache JSON, strict-zip mismatch) is a server bug and must
    propagate to a 500, unswallowed and unleaked."""

    class CorruptPipeline(FakePipeline):
        async def answer_turn(self, query, history=None, session_id=""):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    with pytest.raises(ValueError):
        async with client(pipeline=CorruptPipeline()) as c:
            await c.post("/chat", json={"message": "x"}, headers=AUTH)


async def test_healthz_open_and_reports_redis():
    async with client() as c:
        assert (await c.get("/healthz")).status_code == 200  # no auth required
    async with client(pipeline=FakePipeline(healthy=False)) as c:
        assert (await c.get("/healthz")).status_code == 503


async def test_analytics_requires_auth():
    async with client() as c:
        assert (await c.get("/analytics/summary")).status_code == 401
        assert (await c.get("/analytics/summary", headers=AUTH)).status_code == 200


@pytest.mark.redis
async def test_token_bucket_fixed_window():
    from redis.asyncio import Redis

    r = Redis.from_url("redis://localhost:6379")
    try:
        await r.ping()
    except Exception:
        pytest.skip("redis not running")
    bucket = TokenBucket(r, per_min=3, namespace="mfatest")
    key = "unit-test-key"
    results = [await bucket.allow(key) for _ in range(5)]
    assert results[:3] == [True, True, True] and results[3:] == [False, False]
    async for k in r.scan_iter(match="mfatest:rl:*"):
        await r.delete(k)
    await r.aclose()
