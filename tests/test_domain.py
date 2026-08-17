"""The Fresh/Stale rule (CONTEXT.md) as a table, through its own interface."""

import pytest

from agent.domain import Temporal, is_fresh

NOW = 1_700_000_000.0
DAY = 86400


@pytest.mark.parametrize(
    "ts,temporal,fresh",
    [
        (NOW, Temporal.VOLATILE, False),  # volatile is never fresh, even written just now
        (NOW - 6 * DAY, Temporal.SLOW, True),  # slow: inside the 7-day TTL
        (NOW - 8 * DAY, Temporal.SLOW, False),  # slow: aged out
        (NOW - 7 * DAY, Temporal.SLOW, True),  # slow: TTL boundary is inclusive
        (NOW - 365 * DAY, Temporal.STATIC, True),  # static does not expire
    ],
)
def test_fresh_stale_rule(ts, temporal, fresh):
    assert is_fresh(ts, temporal, now=NOW, slow_ttl_days=7) is fresh
