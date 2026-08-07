# Memory-First Web Agent

A GenAI agent that answers questions from its own memory first and falls back to the live web, ingesting what it finds for future reuse. This glossary is the canonical language — code, logs, documentation, and conversation use these terms identically.

## Memory

**Memory**:
The agent's two-tier Redis-backed store: the Answer Cache plus the Knowledge Base.
_Avoid_: vector store (that names the technology, not the concept), database

**Answer Cache**:
Tier-1 memory mapping previously asked questions to their validated answers and sources. Matched question↔question (symmetric).
_Avoid_: semantic cache, QA cache

**Knowledge Base**:
Tier-2 memory of document chunks ingested from fetched web pages. Matched question↔chunk (asymmetric).
_Avoid_: chunk store, document store

**Chunk**:
A ~800-token markdown segment of a fetched page, carrying provenance (source URL, section, fetch time, content hash).

**Promotion**:
Writing a validated, fully grounded synthesis into the Answer Cache — regardless of whether its sources came from the Knowledge Base or the live web.

**Quarantined**:
A chunk flagged at ingest as containing instruction-like content. Stored with its verdict, excluded from all retrieval.
_Avoid_: blocked, deleted

**Fresh / Stale**:
A memory entry's validity relative to its temporal class: volatile entries are never fresh, slow entries age out after a TTL, static entries do not expire.

**Borderline**:
Knowledge Base similarity between the floor (0.55) and the gate (0.70) — not enough to answer from memory alone, still added as context on the web path.

**Erasure**:
GDPR-grade deletion from memory: by source URL (provenance cascade through both tiers) or by question (Answer Cache key).
_Avoid_: purge, wipe, cleanup (cleanup is routine staleness eviction, not erasure)

**Reconstructible**:
The property that total memory loss only degrades the agent to a plain web agent that re-warms itself through use; memory is never the system of record.

## Turns and Routing

**Turn**:
One query→answer cycle, producing exactly one route and one telemetry record.
_Avoid_: request, query (a query is the turn's input, not the turn)

**Session**:
A conversation holding rolling history (last 10 turns) across turns.
_Avoid_: conversation, chat

**Preflight**:
The single utility-model screening at the start of every turn: injection screen, temporal class, topic tag, PII flag, standalone rewrite.

**Standalone Query**:
The history-resolved, self-contained form of the user's query. All embeddings and cache keys derive from it, never from the raw input.

**Route**:
The turn's outcome class: `hit_cache`, `hit_chunks`, `miss_web`, `degraded`, or `refused`.

**Memory Hit**:
A turn answered from either tier (`hit_cache` or `hit_chunks`).

**Temporal Class**:
The freshness sensitivity of a query: `static` (facts that don't change), `slow` (change over weeks), `volatile` (change constantly; never served from memory).

**PII Gate**:
The preflight decision barring personal-data questions from being written to the Answer Cache. Fails closed (toward not writing).

**Degraded Answer**:
Below-threshold memory content served with an explicit staleness/incompleteness caveat when the web is unavailable. Never cached.

**Grounded**:
The property that every factual claim in an answer is attributable to a cited, actually-retrieved source.

**Hit Rate**:
`(hit_cache + hit_chunks) / answered turns`; refused turns excluded from the denominator, degraded counts as a miss. The system's primary cost and capacity lever.
