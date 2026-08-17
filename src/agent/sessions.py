"""Session — rolling conversation history, last 10 turns (CONTEXT.md: Session).

One module owns the trimming rule; two adapters sit at the seam: Redis-backed
for the HTTP API (1-hour TTL refreshed on activity, spec §12) and in-memory for
the CLI REPL. Hosted mode needs neither — the Responses protocol carries
history itself (ADR-0009)."""

import json

from redis.asyncio import Redis

DEFAULT_MAX_TURNS = 10  # a turn is a user + assistant pair


class SessionStore:
    """Redis adapter: shared across API workers, TTL-bounded."""

    def __init__(
        self,
        redis: Redis,
        namespace: str = "mfa",
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_s: int = 3600,
    ):
        self.r = redis
        self.prefix = f"{namespace}:session:"
        self.max_messages = max_turns * 2
        self.ttl_s = ttl_s

    def _key(self, session_id: str) -> str:
        return self.prefix + session_id

    async def get(self, session_id: str) -> list[dict]:
        # redis-py types hybrid sync/async commands as `Awaitable[T] | T`;
        # on the asyncio client every such call is awaitable, hence the ignores.
        key = self._key(session_id)
        raw = await self.r.lrange(key, -self.max_messages, -1)  # ty: ignore[invalid-await]
        return [json.loads(m) for m in raw]

    async def append(self, session_id: str, user: str, assistant: str) -> None:
        key = self._key(session_id)
        await self.r.rpush(  # ty: ignore[invalid-await]
            key,
            json.dumps({"role": "user", "content": user}),
            json.dumps({"role": "assistant", "content": assistant}),
        )
        await self.r.ltrim(key, -self.max_messages, -1)  # ty: ignore[invalid-await]
        await self.r.expire(key, self.ttl_s)


class InMemorySessionStore:
    """Process-local adapter: same interface and trimming rule, no Redis, no TTL."""

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS):
        self.max_messages = max_turns * 2
        self._data: dict[str, list[dict]] = {}

    async def get(self, session_id: str) -> list[dict]:
        return list(self._data.get(session_id, []))

    async def append(self, session_id: str, user: str, assistant: str) -> None:
        messages = self._data.setdefault(session_id, [])
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
        del messages[: -self.max_messages]
