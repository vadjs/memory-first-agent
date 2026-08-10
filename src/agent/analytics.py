"""Turn-log analytics: hit rate, topics, cost, latency; optional embedding clustering."""

import random
from collections import Counter
from statistics import quantiles

from agent.domain import Route
from agent.telemetry import read_turns

ANSWERED_ROUTES = {Route.HIT_CACHE, Route.HIT_CHUNKS, Route.MISS_WEB, Route.DEGRADED}
HIT_ROUTES = {Route.HIT_CACHE, Route.HIT_CHUNKS}


def summarize(turns: list[dict] | None = None) -> dict:
    turns = read_turns() if turns is None else turns
    routes = Counter(t["route"] for t in turns)
    answered = sum(n for r, n in routes.items() if r in ANSWERED_ROUTES)
    hits = sum(n for r, n in routes.items() if r in HIT_ROUTES)
    latencies = sorted(sum(s["ms"] for s in t["stages"]) for t in turns)

    def percentile(p: int) -> float:
        if len(latencies) > 1:
            return quantiles(latencies, n=100)[p - 1]
        return (latencies or [0.0])[0]

    return {
        "turns": len(turns),
        "routes": dict(routes),
        # Hit rate per spec §10: refused excluded from the denominator, degraded counts as a miss.
        "hit_rate": round(hits / answered, 3) if answered else 0.0,
        "topics": dict(Counter(t["topic"] for t in turns)),
        "temporal": dict(Counter(t["temporal"] for t in turns)),
        "total_cost_usd": round(sum(t["total_cost_usd"] for t in turns), 4),
        "avg_cost_usd": round(sum(t["total_cost_usd"] for t in turns) / len(turns), 5)
        if turns
        else 0.0,
        "latency_p50_ms": round(percentile(50), 1),
        "latency_p95_ms": round(percentile(95), 1),
        "injection_flagged": sum(1 for t in turns if t["injection_flagged"]),
        "pii_flagged": sum(1 for t in turns if t["contains_pii"]),
    }


def _kmeans(vectors: list[list[float]], k: int, iters: int = 20) -> list[int]:
    rng = random.Random(42)
    centroids = [list(v) for v in rng.sample(vectors, k)]
    labels = [0] * len(vectors)
    for _ in range(iters):
        for i, v in enumerate(vectors):
            labels[i] = min(
                range(k),
                key=lambda c: sum((a - b) ** 2 for a, b in zip(v, centroids[c], strict=True)),
            )
        for c in range(k):
            members = [vectors[i] for i in range(len(vectors)) if labels[i] == c]
            if members:
                centroids[c] = [sum(dim) / len(members) for dim in zip(*members, strict=True)]
    return labels


async def cluster_questions(memory, util) -> list[dict]:
    """Emergent topics: k-means over Answer Cache question embeddings, labeled by the
    utility model. Complements the fixed taxonomy (spec §10)."""
    import struct

    from pydantic import BaseModel

    questions: list[str] = []
    vectors: list[list[float]] = []
    async for key in memory.r.scan_iter(match=f"{memory.qa_prefix}*"):
        data = await memory.r.hmget(key, "question", "vec")
        if data[0] and data[1]:
            questions.append(data[0].decode("utf-8", errors="replace"))
            vectors.append(list(struct.unpack(f"{len(data[1]) // 4}f", data[1])))
    if len(questions) < 3:
        return [{"label": "(not enough cached questions to cluster)", "questions": questions}]

    k = min(8, max(2, len(questions) // 3))
    labels = _kmeans(vectors, k)

    class ClusterLabel(BaseModel):
        label: str

    clusters = []
    for c in range(k):
        members = [q for q, label in zip(questions, labels, strict=True) if label == c]
        if not members:
            continue
        try:
            out, _ = await util.complete_json(
                "Name the common theme of these questions in at most 5 words. Return JSON.",
                "\n".join(members[:20]),
                ClusterLabel,
            )
            label = out.label
        except Exception:
            label = "(unlabeled)"
        clusters.append({"label": label, "questions": members})
    return sorted(clusters, key=lambda c: -len(c["questions"]))
