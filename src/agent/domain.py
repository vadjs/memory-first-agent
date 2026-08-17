"""Domain vocabulary (CONTEXT.md) as typed values and rules: routes, temporal
classes, the Fresh/Stale rule, and the Page Summary section marker.

StrEnum members *are* their string values, so JSON serialization, Redis storage,
and comparisons against strings read back from the turn log need no conversion."""

from enum import StrEnum

SUMMARY_SECTION = "[page summary]"
"""Section marker for Page Summary chunks in the Knowledge Base (CONTEXT.md)."""


class Route(StrEnum):
    """The turn's outcome class (CONTEXT.md: Route)."""

    HIT_CACHE = "hit_cache"
    HIT_CHUNKS = "hit_chunks"
    MISS_WEB = "miss_web"
    DEGRADED = "degraded"
    REFUSED = "refused"


class Temporal(StrEnum):
    """The freshness sensitivity of a query (CONTEXT.md: Temporal Class)."""

    STATIC = "static"
    SLOW = "slow"
    VOLATILE = "volatile"


class QueryTooLongError(ValueError):
    """The Pipeline's input invariant (max_query_chars), as a typed contract.

    Adapters translate exactly this type into their surface's rejection (HTTP 422,
    CLI message, chat reply); a blanket ValueError catch would repaint internal
    errors — JSON decode failures, strict-zip mismatches — as client mistakes."""


def is_fresh(ts: float, temporal: Temporal, *, now: float, slow_ttl_days: int) -> bool:
    """The Fresh/Stale rule (CONTEXT.md): volatile entries are never fresh, slow
    entries age out after the TTL, static entries do not expire. The router
    enforces this at read time — freshness-as-routing (ADR-0006)."""
    if temporal == Temporal.VOLATILE:
        return False
    if temporal == Temporal.SLOW:
        return ts >= now - slow_ttl_days * 86400
    return True  # static
