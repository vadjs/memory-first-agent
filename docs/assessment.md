# Well-Architected Self-Assessment

An honest assessment of this implementation against the five pillars of the Azure
Well-Architected Framework, with the gaps named and a prioritized road to production.
The strongest pillar is Cost Optimization (the memory-first design *is* a cost
architecture); the weakest is Reliability.

## Reliability — the weakest pillar, by design honesty

**In place**: per-stage timeouts sized from measured p99s; transport-only retries with
backoff + jitter; per-page fetch failures never fail a turn; explicit degradation ladder
(stale-labeled memory answers when search is down; honest refusal at quota exhaustion);
idempotent memory writes making retries convergent; fast-fail 503 health probe; memory
fully reconstructible after loss (ADR-0002).

**Gaps**: single Redis database is the SPOF (Balanced B0 runs a replica pair internally,
but it is one database in one region); no multi-region story; no load-shedding beyond
rate limiting; conversation history lost if Redis data is lost (acceptable: cache
semantics).

**What breaks first at ~100 concurrent users**: Azure OpenAI TPM quota (spec §3.1 —
~1.8M TPM demanded at 5 QPS all-miss against a 30K TPM utility deployment), well before
Redis (~100K ops/s capable) or the container plane. The mitigation is architectural:
hit rate divides token demand; then quota increase or provisioned throughput.

## Security

**In place**: five-layer injection defense verified by red-team evals; quarantine-not-
rewrite ingestion; citation-subset enforcement; PII gate on shared memory; keyless
managed-identity auth in-cloud; Key Vault references; bearer auth + rate limit + input
cap on the API; admin verbs CLI-only; no secrets in repo, image, or CI (OIDC).

**Gaps**: static bearer key instead of Entra ID authentication (roadmap: Easy Auth or
APIM + Entra); public network paths (roadmap: private endpoints + VNet-injected
Container Apps); no Azure AI Content Safety Prompt Shields yet (blueprint places them at
layers 1–2); LLM-based screens are probabilistic — the deterministic layers bound, but
do not eliminate, classifier error.

## Cost Optimization — the strongest pillar

The architecture's core loop converts spend into an asset: every miss funds memory that
makes future turns ~10–100× cheaper (`docs/cost-model.md`). Per-token-only model billing,
scale-to-zero compute, and the smallest Managed Redis tier keep the idle floor at ~$22/mo.
Cost is observable per turn, per stage, per model in the telemetry. **Gap**: no budget
alerts wired (roadmap: Azure Cost Management alert + a cost-per-answer regression check
in CI's eval job).

## Operational Excellence

**In place**: everything is code — infra (Bicep/azd), quality gates (lint, 91 tests,
deterministic evals, `bicep build` in CI), CD via OIDC with no standing secrets;
structured JSON telemetry per turn; App Insights wiring; the docs you are reading.

**Gaps**: no alert rules or dashboards defined in IaC yet; no SLO dashboards
(the KPIs are defined in `docs/cost-model.md`); groundedness evals are manual-trigger
(cost-gated) rather than nightly.

## Performance Efficiency

**In place**: measured routes — cache hit ~1–3s (dominated by preflight), chunk hit
~5–7s, miss ~8–15s (fetch + synthesis dominated); all page fetches concurrent; ingest
screening parallelized per page; FLAT exact search at sub-ms latencies for this corpus;
reasoning effort pinned to the floor on both models.

**Gaps**: preflight (~1.9s) is the fixed floor on every turn — a smaller classifier or a
partial-parallel scheme (embedding concurrently with preflight) could halve hit-path
latency; no response streaming yet (the API returns complete answers); HNSW migration
point documented but not automated (ADR-0006).

## Prioritized roadmap to production

1. **Entra ID auth on the API** (replaces the static key; biggest security lift for the effort)
2. **Prompt Shields** at input + ingest (managed, adversarially maintained screening)
3. **Streaming responses** end-to-end (largest perceived-latency win)
4. **Budget alerts + nightly eval runs** (cost and quality regression fences)
5. **Private endpoints + VNet injection** (when the estate demands network isolation)
6. **Single-flight collapsing** of concurrent identical misses (correct today via
   idempotency; wasteful under thundering-herd load)
7. **HNSW migration** at the ~50–100K chunk mark (ADR-0006 parameters)
