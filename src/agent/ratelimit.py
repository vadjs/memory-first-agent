"""Per-key request limiting for the HTTP API (spec §12): a leaked key must not
become a cost or quota incident. Fixed-window counter in Redis — coarser than a
true token bucket but two commands per request and race-safe via INCR."""

from redis.asyncio import Redis


class TokenBucket:
    def __init__(self, redis: Redis, per_min: int, namespace: str = "mfa"):
        self.r = redis
        self.per_min = per_min
        self.prefix = f"{namespace}:rl:"

    async def allow(self, key: str) -> bool:
        import time

        window = int(time.time() // 60)
        redis_key = f"{self.prefix}{key}:{window}"
        count = await self.r.incr(redis_key)
        if count == 1:
            await self.r.expire(redis_key, 120)
        return count <= self.per_min
