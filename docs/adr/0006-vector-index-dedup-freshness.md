# FLAT vector index, content-hash keys, freshness enforced at read time

**FLAT, not HNSW**: at this corpus scale (≤ tens of thousands of vectors) exact KNN is
faster in practice than an approximate graph index, with perfect recall and zero build
cost. The switch point is ~50–100K vectors, where HNSW (`M=16`, `EF_CONSTRUCTION=200`)
buys sublinear search for ~1.5× memory and a small recall haircut.

**Content-hash keys**: `sha256(normalized_text)` keys chunks and `sha256(normalized_question)`
keys cache entries, so identical content upserts instead of duplicating — ingestion is
idempotent, which is what makes retries and concurrent identical turns converge without
any locking (ADR-0002). URL-level markers (TTL'd) skip re-fetching recently ingested pages.

**Freshness-as-routing**: entries carry timestamps and never self-expire; the router
decides validity per query using the preflight temporal class (volatile → memory never
serves; slow → 7-day TTL; static → no expiry). Storage-level TTLs were rejected because
deleting at write time couples staleness policy to erasure policy — keeping them apart is
what lets `memory cleanup` (staleness) and `memory forget` (GDPR erasure) stay independent.
