from agent.analytics import cluster_questions, summarize


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


class FakeQAMemory:
    """Satisfies the one interface clustering consumes: iter_cached_questions."""

    def __init__(self, items):
        self.items = items

    async def iter_cached_questions(self):
        for question, vec in self.items:
            yield question, vec


class FakeLabeler:
    async def complete_json(self, system, user, schema):
        return schema(label=f"theme of {user.splitlines()[0]}"), None


async def test_cluster_questions_groups_by_vector():
    items = [
        ("what is redis", [1.0, 0.0]),
        ("explain redis persistence", [0.99, 0.05]),
        ("redis vs memcached", [0.98, 0.1]),
        ("how do plants grow", [0.0, 1.0]),
        ("why are leaves green", [0.05, 0.99]),
        ("what is photosynthesis", [0.1, 0.98]),
    ]
    clusters = await cluster_questions(FakeQAMemory(items), FakeLabeler())
    assert len(clusters) == 2
    assert sorted(len(c["questions"]) for c in clusters) == [3, 3]
    assert all(c["label"].startswith("theme of") for c in clusters)


async def test_cluster_questions_too_few():
    clusters = await cluster_questions(FakeQAMemory([("only one", [1.0, 0.0])]), FakeLabeler())
    assert clusters[0]["questions"] == ["only one"]
