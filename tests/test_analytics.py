from agent.analytics import summarize


def turn(route, topic="technology", temporal="static", cost=0.01, ms=1000.0, **extra):
    return {
        "route": route,
        "topic": topic,
        "temporal": temporal,
        "total_cost_usd": cost,
        "stages": [{"stage": "all", "ms": ms}],
        "injection_flagged": extra.get("injection_flagged", False),
        "contains_pii": extra.get("contains_pii", False),
    }


def test_hit_rate_excludes_refused_and_counts_degraded_as_miss():
    turns = [
        turn("hit_cache"),
        turn("hit_chunks"),
        turn("miss_web"),
        turn("degraded"),
        turn("refused"),  # excluded from the denominator
    ]
    s = summarize(turns)
    assert s["hit_rate"] == 0.5  # 2 hits / 4 answered
    assert s["turns"] == 5
    assert s["routes"]["refused"] == 1


def test_costs_and_topics_aggregate():
    turns = [turn("miss_web", cost=0.01), turn("hit_cache", topic="health", cost=0.0002)]
    s = summarize(turns)
    assert s["total_cost_usd"] == 0.0102
    assert s["topics"] == {"technology": 1, "health": 1}


def test_empty_log():
    s = summarize([])
    assert s == {
        "turns": 0,
        "routes": {},
        "hit_rate": 0.0,
        "topics": {},
        "temporal": {},
        "total_cost_usd": 0.0,
        "avg_cost_usd": 0.0,
        "latency_p50_ms": 0.0,
        "latency_p95_ms": 0.0,
        "injection_flagged": 0,
        "pii_flagged": 0,
    }
