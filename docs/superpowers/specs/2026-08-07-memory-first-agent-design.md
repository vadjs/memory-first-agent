# Memory-First Web Agent — Design Specification

| | |
|---|---|
| **Status** | Draft for review |
| **Date** | 2026-08-07 |
| **Author** | Vadim Zhamkov |
| **Process** | Spec-first, AI-assisted workflow (see `docs/ai-assistance.md`) |

## 1. Overview

A GenAI agent that answers user questions **memory-first**: it checks a Redis vector memory before touching the web. On a miss it searches the web, fetches and converts pages to markdown, ingests them into memory, and answers from the retrieved context. Every answer is grounded and cites source URLs. Memory turns repeated and related questions from a multi-second, LLM-heavy web pipeline into a sub-second, low-cost retrieval — memory hit rate is the system's primary cost and latency lever.

## 2. Functional Requirements

FR-1. Embed the user query and perform vector search in Redis before any web access.
FR-2. If the top result similarity ≥ threshold (default **0.7**, configurable), answer from memory only, including stored metadata (source URLs, timestamps).
FR-3. On memory miss: search the web, fetch top result pages, convert them to **markdown**, summarize/chunk, store chunks + metadata in Redis, then answer from the retrieved context.
FR-4. Answers include clear metadata: the source URLs actually used.
FR-5. Two LLMs with distinct roles: a **conversation** model (answer synthesis) and a **utility/analytics** model (routing classification, guardrail screening, summarization, topic tagging). Choice justified by cost and quality (§6, ADR-003).
FR-6. Log every turn: memory **hit** or **miss + web search**, with full turn telemetry (§10).
FR-7. Analytics on the topics and types of questions users ask (§10).
FR-8. Multi-turn conversation: a chat REPL with rolling history; queries are rewritten to standalone form before embedding.

## 3. Non-Functional Requirements

| NFR | Requirement | Design response |
|---|---|---|
| Security | Prompt-injection guardrails | Defense-in-depth, §8 |
| Reliability | Timeouts/retries for network and token issues | Per-stage budgets, idempotent retries, graceful degradation, §9 |
| Observability | Turn-level telemetry | Structured JSON logs, cost/latency per stage, §10 |
| Quality | Grounded, non-hallucinated answers | Citation validation + evaluation suite, §11 |
| Cost | Predictable per-turn economics | Model tiering + semantic caching, §6, `docs/cost-model.md` |
| Portability | No hard cloud lock-in | Provider-agnostic config; Azure primary, OpenAI-direct fallback; multi-cloud mapping in `docs/blueprint.md` |
| Compliance | GDPR alignment; operable within an ISO 27001 ISMS | Compliance by design, §8.6; `docs/security.md` |

## 4. Architecture

### 4.1 Components

```mermaid
flowchart TB
    subgraph Client
        CLI[CLI - chat / ask / analytics / memory]
    end
    subgraph Agent["Agent Core (MS Agent Framework workflow)"]
        PRE["Preflight: guardrail screen,<br/>temporal class, topic tag, rewrite"]
        ROUTE{Router}
        SYN["Answer Synthesis"]
        VAL["Output Validation"]
    end
    subgraph Memory["Memory (Redis 8, vector search)"]
        QA[("qa_cache index<br/>Q→A pairs")]
        CH[("chunks index<br/>document chunks")]
    end
    subgraph Web["Web Pipeline"]
        SEARCH[Tavily Search]
        FETCH[Async page fetch]
        MD[Markdown conversion]
        SCREEN[Ingest guardrail screen]
        CHUNK[Chunk + embed + upsert]
    end
    LLM1[Conversation LLM]
    LLM2[Utility LLM]
    EMB[Embeddings]

    CLI --> PRE --> ROUTE
    ROUTE -->|cache hit| QA --> VAL
    ROUTE -->|chunk hit| CH --> SYN
    ROUTE -->|miss| SEARCH --> FETCH --> MD --> SCREEN --> CHUNK --> SYN
    SYN --> VAL --> CLI
    PRE -.-> LLM2
    SCREEN -.-> LLM2
    SYN -.-> LLM1
    ROUTE -.-> EMB
```

### 4.2 Turn flow

1. **Preflight** (one utility-LLM call): injection screen on user input, temporal-intent class (`static` / `slow` / `volatile`), topic tag (fixed taxonomy + `other`), standalone query rewrite using conversation history.
2. **Route** — embed the standalone query, then:
   - `volatile` query → skip memory read, go to web (results still ingested).
   - **Tier 1 — semantic answer cache**: search `qa_cache` (question↔question, symmetric). Similarity ≥ `CACHE_THRESHOLD` (0.85) and fresh per §5.3 → return stored answer + sources. Log `HIT/cache`.
   - **Tier 2 — chunk memory**: search `chunks` (question↔chunk, asymmetric). Top-1 ≥ `CHUNK_THRESHOLD` (0.70, the task default) and fresh → synthesize from top-k (k=5) chunks. Log `HIT/chunks`.
   - **Miss** → web pipeline: Tavily search (top 5) → fetch up to 3 pages concurrently → markdown → ingest screen → chunk/embed/upsert → synthesize from fresh chunks **plus** any borderline memory chunks (similarity 0.55–0.70). Store the new Q→A pair in `qa_cache`. Log `MISS/web`.
3. **Synthesize** (conversation LLM): answer strictly from provided context; retrieved content is wrapped as delimited untrusted data; every claim must be attributable; sources listed.
4. **Validate**: cited URLs must be a subset of retrieved URLs; guardrail verdicts attached; turn telemetry written.

### 4.3 Orchestration

Microsoft **Agent Framework** (Python, `agent-framework`) — workflow graph with executors for preflight, routing, web pipeline, and synthesis; Azure OpenAI chat clients. Rationale and the LangGraph trade-off: ADR-001. Contingency: if the workflow API blocks progress, the same graph runs as a plain asyncio pipeline behind identical component interfaces, with Agent Framework retained for model access (noted in ADR-001; component boundaries make the swap invisible to the rest of the system).

## 5. Memory Design

### 5.1 Two tiers, two thresholds

Question↔question matching (Tier 1) is symmetric: paraphrases of the same question embed close together, so a high threshold (0.85) gives precise, near-free repeat answers. Question↔chunk matching (Tier 2) is asymmetric — questions and answer-bearing prose are different text types with systematically lower cosine similarity — so the gate stays at the task default 0.70, deliberately conservative: a redundant web search is cheaper than a misleading answer. Thresholds are per-index, per-embedding-model, configurable, and calibrated empirically (ADR-002). Redis returns cosine *distance*; the memory layer normalizes to similarity in one place.

### 5.2 Indexes (Redis 8, RediSearch, FLAT, cosine, 1536 dims)

- `qa_cache`: question text + embedding → answer, source URLs, `topic`, `temporal_class`, `created_at`, `hit_count`, `last_hit_at`.
- `chunks`: chunk text + embedding → source URL, title, section, `content_sha256`, `fetched_at`, `ingest_flags` (guardrail verdicts), `hit_count`, `last_hit_at`.

**FLAT, not HNSW**: at POC scale (≤ tens of thousands of vectors) exact search is faster in practice, has perfect recall, and zero build cost; the switch point (~50–100K vectors) and HNSW parameters are documented in ADR-004.

### 5.3 Freshness

Freshness is a **routing** concern, not a storage concern. Memory entries carry timestamps; the preflight temporal class decides validity at query time: `volatile` → never served from memory; `slow` → valid within `SLOW_TTL_DAYS` (7); `static` → no expiry. Answers surface source dates. Native Redis TTL is not used for vectors (too blunt); a `memory cleanup` command evicts stale/cold entries.

### 5.4 Deduplication and idempotency

- **URL level**: normalized URL (scheme/host lowercased, tracking params and fragments stripped) hashed; a URL fetched within its TTL is not re-ingested.
- **Content level**: `sha256(normalized_chunk_text)` is part of the Redis key → identical chunks upsert instead of duplicating, making ingestion idempotent and therefore safely retryable.

## 6. Model Selection and Cost

All models served from one Azure AI Foundry resource; provider-agnostic env config allows an OpenAI-direct fallback with zero code change (ADR-003).

| Role | Model | Why |
|---|---|---|
| Conversation | `gpt-5.6-luna` (low/no reasoning effort) | Newest generation (Jul 2026), GA in Foundry across all global regions. $0.20/$1.20 per 1M tokens — ~6–8× cheaper than `gpt-5.1` ($1.25/$10) while newer and faster (~190 tok/s); strong knowledge/low-hallucination scores; 1M context. Run at low reasoning effort for chat-grade latency. |
| Utility/analytics | `gpt-5-nano` | Preflight classification, injection screening, topic tagging are structurally simple tasks on the critical path of **every** turn — a small non-reasoning model minimizes time-to-first-token and costs $0.05/$0.40. Note `gpt-5-mini` is *not* cheaper than Luna ($0.25/$2 vs $0.20/$1.20), which rules it out of both roles. |
| Embeddings | `text-embedding-3-small` (1536) | `-large` is ~6.5× the cost for marginal gain at this scale. |

Model bindings are deployment-name environment config: swapping a model, or the provider itself (Azure ↔ OpenAI direct), requires no code change — that is the resilience mechanism, so no static fallback models are designated. `gpt-5.6-terra` is the documented quality-upgrade path (ADR-003).

Considered and rejected: `gpt-5.6-sol` ($5/$30) — a frontier reasoning model for long-horizon agentic work; grounded synthesis from retrieved context does not use that depth, and reasoning-heavy decoding hurts chat latency. `gpt-5.6-terra` ($2/$12) — 10× Luna's price for marginal gain on this workload. `gpt-5.1` — dominated by Luna on price, speed, and recency at comparable quality (full comparison in ADR-003).

Indicative per-turn economics (list prices, production rates):

| Component | Miss-path turn | Cache-hit turn |
|---|---|---|
| Tavily search (basic, 1 credit @ $0.008) | 0.8¢ | — |
| Tavily Extract fallback (~0.2 credits/page, occasional) | ~0–0.2¢ | — |
| Conversation LLM (Luna, ~5K in / 500 out) | ~0.16¢ | — |
| Utility LLM + embeddings | <0.02¢ | <0.02¢ |
| **Total** | **≈ 1¢** | **≈ 0.02¢** |

The search API — not the LLM — dominates miss-path cost by ~4–8×, which sharpens the business case: every memory hit avoids the single most expensive component entirely, making hit rate a ~50× cost multiplier between the paths. Full sensitivity analysis: `docs/cost-model.md`. Model availability and quota are verified at scaffold time; actually-deployed models are recorded in ADR-003.

## 7. Web Acquisition Pipeline

- **Search**: Tavily API, top 5 results (metadata only — content acquisition is a separate, swappable stage).
- **Fetch**: behind a `ContentFetcher` interface with two implementations. **Primary**: `httpx` async + `trafilatura` markdown conversion — all results fetched concurrently, 10s per page; wall time is bounded by the slowest page, not the page count. **Fallback**: Tavily Extract (markdown format) for pages the direct fetch cannot serve — bot-protected or JS-rendered sites — invoked per-page on failure. Either implementation can be made primary via config. Per-page failures skip the page, never the turn; the turn proceeds once at least one page succeeds.
- Tavily **Map/Crawl** are deliberately not used: they serve whole-site ingestion (corpus seeding), not per-question acquisition.
- **Chunking**: heading-aware markdown splitting, target ~800 tokens, ~15% overlap; section title kept in metadata.
- **Ingest screen**: §8, layer 2.

## 8. Security Design (prompt injection)

Threat model (details in `docs/security.md`): the highest-risk vector is **indirect** injection — instructions embedded in fetched web pages that would otherwise persist in memory and re-serve themselves ("memory poisoning"). Defense-in-depth; no single layer is trusted:

1. **Input screen** (preflight, utility LLM): direct-injection classification of user input.
2. **Ingest screen** (web path): structural stripping first (markdown conversion drops scripts; zero-width chars, HTML comments, base64 blobs removed), then the utility LLM **classifies** chunks for instruction-like content — classify-and-quarantine, never rewrite-and-trust (a sanitizer that rewrites poisoned text can itself be injected). Flagged chunks are stored quarantined (excluded from retrieval) with verdicts in metadata.
3. **Prompt architecture**: instruction hierarchy (system > user > data) plus spotlighting — retrieved content is delimited as untrusted data the model must treat as citable material, never as instructions. Hierarchy fine-tuning reduces injection success but does not eliminate it; it is one layer, not the control.
4. **Output validation**: citations must be a subset of actually-retrieved URLs (blocks fabricated sources); refusal template on guardrail trip.
5. **Least privilege**: the agent has no side-effecting tools retrieved content could trigger; the blast radius of a successful injection is a bad answer, which layers 4 and the evaluation suite (§11) are designed to catch.

Production hardening (Azure AI Content Safety Prompt Shields at layers 1–2): `docs/blueprint.md`.

### 8.6 Compliance by design (GDPR, ISO 27001)

**GDPR.** Personal data can enter through two doors: user queries (may contain PII) and ingested web content (third-party PII). Controls:

- **Data minimization**: no user identity is collected; turn logs store query text and telemetry only, keyed by opaque `turn_id`.
- **Storage limitation**: freshness TTLs (§5.3) and `memory cleanup` bound retention; retention defaults documented in `docs/security.md`.
- **Right to erasure by design**: every chunk carries provenance (source URL, content hash) and every cache entry records its source URLs, so `agent memory forget --url <URL>` (or a query-matched variant) cascades deletion through both indexes — erasure works *because* provenance metadata exists. This is a known hard problem in vector stores; the design solves it structurally rather than by full reindex.
- **Data residency**: Azure deployment pins an EU region (e.g., Sweden Central); Azure OpenAI processes in-geography via EU Data Zone deployments and does not train on customer data (Microsoft DPA applies).
- **Processor transparency**: search queries transit Tavily (third-party processor) — documented in `docs/security.md`; production alternative with EU processing noted in `docs/blueprint.md`. Lawful basis and DPIA are operator responsibilities; the repo documents the data flows they need.

**ISO 27001.** The standard certifies an organization's ISMS, not a piece of software — the correct claim is that the solution is designed to **operate within an ISO 27001-certified ISMS** and maps to Annex A controls: cryptographic controls (TLS in transit; encryption at rest on Azure; Redis TLS/AUTH in production), access control (Key Vault, managed identity, RBAC — no secrets in code or repo), logging and monitoring (turn telemetry, Application Insights), secure development (CI quality gates, dependency pinning, reviewed changes), and supplier security (Microsoft, OpenAI, and Tavily each hold ISO 27001 certification). The control mapping table lives in `docs/security.md`.

## 9. Reliability Design

Timeouts bound **failure detection**, not expected latency: each is sized ~3× the provider's typical p99 and tuned after first measurements. All values are config.

| Stage | Timeout | Typical latency | Retry policy |
|---|---|---|---|
| Tavily search | 5s | ~1–2s (basic depth) | 3× exponential backoff + jitter (idempotent) |
| Page fetch | 10s/page, all concurrent | 1–5s | 2× per page; failures skip the page |
| Embeddings | 10s | ~0.1–0.5s single, 1–2s batched | 3× backoff (idempotent) |
| Utility LLM (nano) | 15s | ~1s | 3× backoff on 429/5xx/transport only — never on "bad output" |
| Conversation LLM (streamed) | 30s total, 10s to first token | ~3–5s | same as utility |
| Redis ops | 2s | ~1–10ms | 2× backoff; writes idempotent via content-hash keys |

Degradation: if web search is unavailable on a miss, the agent serves the best below-threshold memory content **explicitly labeled** as potentially stale/incomplete, and refuses only when memory has nothing relevant (`DEGRADED_ANSWERS=true` default; strict-refusal available via config — ADR-006). Retries live in one decorator module (`tenacity`), not scattered.

## 10. Observability and Analytics

Every turn emits one structured JSON record (stdout + `logs/turns.jsonl`): `turn_id`, route (`HIT/cache` | `HIT/chunks` | `MISS/web`), topic, temporal class, guardrail verdicts, top-k similarity scores, per-stage latencies, token usage and computed cost per model, cited URLs. Implemented with `structlog`; optional Langfuse tracing behind an env flag.

Analytics (FR-7): **online** — the preflight call tags every turn with a topic (10-class taxonomy + `other`); **offline** — `agent analytics` aggregates hit rate, cost, latency percentiles, and topic/intent distributions from the logs, and can cluster stored query embeddings to surface emergent topics inside `other`.

Primary business metric: memory hit rate (the cost lever). Primary paging metrics (production): error rate and p95 latency.

## 11. Evaluation Strategy

Offline suite in `evals/`, wired into CI:

1. **Routing tests** (deterministic, mocked embeddings/search): seeded memory, known queries → assert hit/miss decisions, threshold edges, volatile bypass, degradation path.
2. **Citation validity** (deterministic): answer URLs ⊆ retrieved URLs on every golden-set run.
3. **Groundedness** (LLM-as-judge, utility model; runs only when a key is present): faithfulness of answers to retrieved context on a ~15-question golden set.
4. **Injection red team**: fixture pages and queries with embedded attacks → assert screens flag/quarantine and the answer path stays clean.

Online complement (roadmap): thumbs up/down feedback building a human-reviewed dataset.

## 12. Interfaces

Typer CLI, Rich output: `agent chat` (REPL), `agent ask "…"` (one-shot), `agent analytics [--cluster]`, `agent memory stats|cleanup|clear|forget --url <URL>` (the `forget` verb implements GDPR erasure, §8.6). Each answer shows the route badge (memory hit / web), sources with dates, and per-turn cost at `-v`.

## 13. Configuration

`pydantic-settings`, `.env` (never committed; `.env.example` provided): Azure/OpenAI endpoint + key + deployment names, Tavily key, Redis URL, thresholds, TTLs, timeouts, feature flags (`DEGRADED_ANSWERS`, `LANGFUSE_*`). Provider switch = env change only.

## 14. Infrastructure, IaC, CI

- **Local**: `docker-compose.yml` — `redis:8` + the agent container; `Dockerfile` (uv-based, multi-stage). One command: `docker compose up`.
- **Azure (azd)**: `azure.yaml` + `infra/main.bicep` — Container Apps environment + app, Azure Managed Redis, Azure AI Foundry account with the three model deployments, Log Analytics + Application Insights, Key Vault; managed identity + RBAC (no keys in app config). `azd provision` deploys the reference environment; the repo does not require deployment (`bicep build` validation runs in CI). Terraform alternative discussed in ADR-005.
- **CI (GitHub Actions)**: ruff (lint+format), pytest (unit + deterministic evals), `bicep build` validation. Keyed evals (groundedness) as a manual workflow.
- **Python 3.13** (`uv`-managed): newest release line verifiably supported by the full dependency chain (Agent Framework, trafilatura/lxml, redis-py). 3.14 is attempted at scaffold time — a one-line change under uv — and adopted if resolution and tests pass; an I/O-bound LLM workload gains nothing from 3.14's headline features, so it is not worth pre-interview risk (ADR-005).

## 15. Repository Layout

```
├── src/agent/            # the installable `agent` package (src layout); config, memory, web, guardrails, workflow, cli, telemetry
├── evals/                # golden set, fixtures, eval runners
├── tests/                # unit tests
├── infra/                # main.bicep + modules; azure.yaml at root
├── docs/
│   ├── SAD.md            # architecture views: context, C4, sequences, deployment, QA scenarios
│   ├── adr/              # ADR-001..006
│   ├── blueprint.md      # production reference architecture (Azure) + multi-cloud mapping
│   ├── assessment.md     # Well-Architected self-assessment + roadmap to production
│   ├── cost-model.md     # per-turn economics, hit-rate sensitivity, KPIs
│   ├── security.md       # threat model and guardrail design
│   ├── ai-assistance.md  # how AI assistance was used (task requirement)
│   └── superpowers/      # this spec + the implementation plan
├── docker-compose.yml, Dockerfile, azure.yaml
└── .github/workflows/ci.yml
```

ADRs: 001 orchestration framework · 002 two-tier memory & thresholds · 003 model selection & cost · 004 vector index, dedup & freshness · 005 runtime & IaC choices · 006 reliability & degradation policy.

## 16. Out of Scope

Web UI; user auth/multi-tenancy; hosted deployment (repo-only per the task); fine-tuning; online feedback loop (roadmap); Prompt Shields integration (production blueprint only).

## 17. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Model quota/availability limits on trial subscription | GPT-5.6 is GA in all global regions on Global Standard; quota verified at scaffold; config-level model swap if needed; ADR-003 records deployed reality |
| Agent Framework workflow API friction | Thin orchestration layer; asyncio fallback behind same interfaces (§4.3) |
| Extraction failures on some sites | trafilatura → readability fallback; per-page skip (§9) |
| Tavily free-tier limits | ~1K credits/month ≫ demo needs; retries + clear error surfacing |
| Azure trial credit burn | Standard (per-token) deployments only; no idle-billing SKUs; total projected spend < $5 |

The subscription- and quota-related notes above are working context for this spec and the implementation plan only; they do not appear in the solution documentation (`README`, `docs/SAD.md`, `docs/blueprint.md`, etc.).
