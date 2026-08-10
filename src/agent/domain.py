"""Domain vocabulary (CONTEXT.md) as typed values: routes and temporal classes.

StrEnum members *are* their string values, so JSON serialization, Redis storage,
and comparisons against strings read back from the turn log need no conversion."""

from enum import StrEnum


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
