"""Redis-backed rolling conversation history for the HTTP API (spec §12):
last 10 turns per session, 1-hour TTL refreshed on activity."""

import json

from redis.asyncio import Redis


class SessionStore:
    def __init__(
        self, redis: Redis, namespace: str = "mfa", max_turns: int = 10, ttl_s: int = 3600
    ):
        self.r = redis
        self.prefix = f"{namespace}:session:"
        self.max_messages = max_turns * 2  # a turn is a user + assistant pair
        self.ttl_s = ttl_s

    def _key(self, session_id: str) -> str:
        return self.prefix + session_id

    async def get(self, session_id: str) -> list[dict]:
        raw = await self.r.lrange(self._key(session_id), -self.max_messages, -1)
        return [json.loads(m) for m in raw]

    async def append(self, session_id: str, user: str, assistant: str) -> None:
        key = self._key(session_id)
        await self.r.rpush(
            key,
            json.dumps({"role": "user", "content": user}),
            json.dumps({"role": "assistant", "content": assistant}),
        )
        await self.r.ltrim(key, -self.max_messages, -1)
        await self.r.expire(key, self.ttl_s)
