# Memory is a reconstructible cache with bounded staleness

Nothing in the system requires strong consistency or durable memory: chunks are re-fetchable derived data, cache entries are cache-semantic, and the append-only turn log (JSONL / Application Insights) is the actual system of record. We decided the system prioritizes availability with bounded staleness — answers may lag the live web within temporal-class TTLs by design — with idempotent, content-hash-keyed writes so retries and concurrent turns converge; persistence stays on (local AOF everysec, Managed Redis persistence in production) but RPO is deliberately relaxed, since total memory loss merely degrades the agent to a plain web agent that re-warms itself.

## Consequences

- Single-node Redis is an accepted POC single point of failure: the API fast-fails 503 via `/healthz` rather than hanging; production posture is Managed Redis replication plus ≥2 app replicas.
- There is no backfill or backup-restore problem to solve; "restore" is normal operation.
- Freshness is enforced at read time (routing), never by deleting data at write time — which is also what makes erasure and staleness independently tunable.
