# Memory-First Web Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans — **inline execution, the chosen mode for this project**: a single agent holds the accumulated design context and the live cloud session, and per-task commits + tests provide the review ratchet. (Subagent-driven development was considered and declined; twelve cold starts add overhead without adding safety here.) Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the memory-first web agent per `docs/superpowers/specs/2026-08-07-memory-first-agent-design.md` — working code, evals, IaC, CI, and the documentation pack.

**Architecture:** Plain-async pipeline (`answer_turn`) orchestrating small, independently testable components: preflight guardrail → two-tier Redis vector routing → web acquisition → grounded synthesis → output validation. MS Agent Framework provides the model/agent abstractions; routing logic stays framework-independent. Azure OpenAI (Foundry) serves all three models. Two entrypoints over the same pipeline: Typer CLI (local) and FastAPI HTTP API (production on Azure Container Apps, deployed via azd with CI/CD).

**Tech Stack:** Python 3.13 (try 3.14 at scaffold), uv, agent-framework, redis-py (RediSearch), httpx, trafilatura, tavily-python, typer, rich, fastapi, uvicorn, structlog, tenacity, pydantic-settings, tiktoken, pytest, ruff, Bicep + azd, GitHub Actions (CI + CD via OIDC).

## Global Constraints

- Python `>=3.13`; attempt 3.14 at scaffold — adopt only if `uv sync` + full test pass.
- All commits authored as **Vadim Zhamkov <vadim.zhamkov@gmail.com>** — no AI attribution anywhere in git history.
- Secrets only in `.env` (gitignored). Never in code, docs, logs, or fixtures.
- `.desc/` never committed.
- No mention of trial-subscription/quota context or access arrangements in any solution-facing doc (README, SAD, blueprint, assessment, cost-model, security).
- Thresholds/config defaults: `CACHE_THRESHOLD=0.85`, `CHUNK_THRESHOLD=0.70`, `BORDERLINE_FLOOR=0.55`, `SLOW_TTL_DAYS=7`, `TOP_K=5`, `RATE_LIMIT_PER_MIN=30`, `MAX_QUERY_CHARS=2000`.
- Repository: `vadjs/memory-first-agent` (private GitHub).
- Memory content policy (spec §5.5, ADR-0001): every validated synthesis is promoted to the Answer Cache **except** volatile/degraded/refused/PII-flagged turns; preflight PII flag fails closed (`contains_pii=true` on parse failure).
- Ubiquitous language per `CONTEXT.md` (Answer Cache, Knowledge Base, Promotion, Quarantined, …) — use in code identifiers, log fields, and docs.
- ADRs: `docs/adr/0001`–`0002` exist (design-time); implementation ADRs are `0003`–`0008`.
- Hit rate = `(hit_cache + hit_chunks) / answered turns`; refused excluded, degraded counts as miss.
- Models: chat `gpt-5.6-luna` (low reasoning effort), utility `gpt-5-nano`, embeddings `text-embedding-3-small` (1536 dims). Azure region: `swedencentral`.
- Topic taxonomy (10): `technology, science, health, business_finance, news_politics, travel_geography, sports_entertainment, howto_practical, culture_history, other`.
- Redis keys: chunks `mfa:chunk:{sha256(text)}`, cache `mfa:qa:{sha256(normalized_question)}`; indexes `idx:chunks`, `idx:qa`; HASH storage, FLAT, COSINE, FLOAT32.
- Prices per 1M tokens (cost constants): luna $0.20/$1.20, nano $0.05/$0.40, embed $0.02.
- Test markers: `-m "not external"` must pass with no network and no keys; `external` marks tests needing Azure/Tavily; `redis` marks tests needing dockerized Redis.

---

### Task 0: Scaffold, tooling, containers

**Files:**
- Create: `pyproject.toml`, `.python-version`, `ruff.toml`, `.env.example`, `docker-compose.yml`, `Dockerfile`, `src/agent/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`

**Steps:**

- [ ] `uv init --package --name agent` reshaped to src layout; set `requires-python = ">=3.13"`.
- [ ] `uv add agent-framework redis httpx trafilatura tavily-python typer rich fastapi uvicorn structlog tenacity pydantic-settings tiktoken openai azure-identity` and `uv add --dev pytest pytest-asyncio respx ruff fakeredis`. If `agent-framework` needs a pre-release: `uv add agent-framework --prerelease allow`. Verify import path for the Azure OpenAI chat client (`python -c "import agent_framework; ..."`) against current docs before Task 6.
- [ ] Try Python 3.14: set `.python-version` to `3.14`, `uv sync`; on any resolution/wheel failure revert to `3.13` and note the blocker in ADR-005 material.
- [ ] `tests/test_smoke.py`: `def test_import(): import agent` — run `uv run pytest`, expect PASS.
- [ ] `docker-compose.yml`: service `redis` = `redis:8` with `command: redis-server --appendonly yes` (ADR-0002), port 6379, healthcheck `redis-cli ping`; service `agent` = build from `Dockerfile` (multi-stage uv build), profile `app` so `docker compose up` alone starts only Redis for dev.
- [ ] `ruff.toml`: target py313, `select = ["E","F","I","UP","B"]`, line length 100. Run `uv run ruff check .` → clean.
- [ ] `.env.example`: all config keys from the spec §13 with placeholder values and one-line comments.
- [ ] Commit: `chore: scaffold project, tooling, and local containers`

### Task 1: Config and telemetry/cost core

**Files:**
- Create: `src/agent/config.py`, `src/agent/telemetry.py`, `tests/test_config.py`, `tests/test_telemetry.py`

**Interfaces (produced):**
```python
# config.py
class Settings(BaseSettings):  # env-driven, .env loaded
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    chat_deployment: str = "gpt-5.6-luna"
    utility_deployment: str = "gpt-5-nano"
    embed_deployment: str = "text-embedding-3-small"
    tavily_api_key: str = ""
    redis_url: str = "redis://localhost:6379"
    cache_threshold: float = 0.85
    chunk_threshold: float = 0.70
    borderline_floor: float = 0.55
    slow_ttl_days: int = 7
    top_k: int = 5
    degraded_answers: bool = True
    fetch_timeout_s: float = 10.0
    search_timeout_s: float = 5.0
    llm_timeout_s: float = 30.0
    utility_timeout_s: float = 15.0
    api_key: str = ""            # bearer key for the HTTP API; empty disables auth (local dev)
    use_managed_identity: bool = False  # True in Azure: azure-identity token auth instead of api key
    rate_limit_per_min: int = 30
    max_query_chars: int = 2000
def get_settings() -> Settings  # cached

# telemetry.py
class StageTiming(TypedDict): stage: str; ms: float
@dataclass class Usage: model: str; input_tokens: int; output_tokens: int
def cost_usd(usage: Usage) -> float  # from PRICES table
@dataclass class TurnRecord:
    turn_id: str; query: str; route: Literal["hit_cache","hit_chunks","miss_web","refused","degraded"]
    topic: str; temporal: str; injection_flagged: bool
    scores: dict[str, float]; stages: list[StageTiming]
    usages: list[Usage]; total_cost_usd: float; cited_urls: list[str]
def log_turn(rec: TurnRecord) -> None  # structlog JSON + logs/turns.jsonl
def read_turns() -> list[dict]  # for analytics
```

**Steps:**

- [ ] Failing tests: settings read from env (monkeypatch), defaults correct; `cost_usd(Usage("gpt-5.6-luna", 1_000_000, 1_000_000)) == pytest.approx(1.40)`; `log_turn` writes one JSON line readable by `read_turns`.
- [ ] Implement; `PRICES` dict keyed by model name with `(in_per_m, out_per_m)`.
- [ ] `uv run pytest tests/test_config.py tests/test_telemetry.py -v` → PASS. Commit: `feat: config and turn telemetry with cost accounting`

### Task 2: Azure provisioning (operational — CLI, no TDD)

**Steps:**

- [ ] `az group create -n mfa-rg -l swedencentral`
- [ ] `az cognitiveservices account create -n mfa-foundry -g mfa-rg --kind AIServices --sku S0 -l swedencentral --custom-domain mfa-foundry`
- [ ] Discover exact model versions: `az cognitiveservices model list -l swedencentral -o table | grep -iE "5.6-luna|5-nano|embedding-3-small"`
- [ ] Three `az cognitiveservices account deployment create` calls (`--sku-name GlobalStandard`, capacity from available quota; deployment name = model name).
- [ ] Write endpoint + key into `.env` (`az cognitiveservices account show/keys list`).
- [ ] Smoke script `scripts/smoke_azure.py`: one chat call on each deployment + one embedding; prints latency + token usage. Run → all three respond.
- [ ] No commit of secrets; commit only `scripts/smoke_azure.py`: `chore: azure smoke-test script`

### Task 3: Embeddings + Redis memory layer

**Files:**
- Create: `src/agent/embeddings.py`, `src/agent/memory.py`, `tests/test_memory.py`

**Interfaces (produced):**
```python
# embeddings.py
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
class AzureEmbedder(Embedder): ...   # openai AsyncAzureOpenAI, retries via tenacity
class FakeEmbedder(Embedder): ...    # deterministic hash-based vectors, for tests/evals

# memory.py
@dataclass class ChunkHit: text: str; url: str; title: str; section: str; fetched_at: float; similarity: float
@dataclass class CacheHit: question: str; answer: str; urls: list[str]; created_at: float; similarity: float
class MemoryStore:
    def __init__(self, redis_url: str, embedder: Embedder): ...
    async def ensure_indexes(self) -> None
    async def search_cache(self, query: str) -> CacheHit | None          # top-1 w/ similarity
    async def search_chunks(self, query: str, k: int) -> list[ChunkHit]
    async def upsert_chunks(self, chunks: list[dict]) -> int             # dedup via sha256 key; skips quarantined
    async def put_qa(self, question: str, answer: str, urls: list[str], topic: str, temporal: str) -> None
    async def url_recently_ingested(self, url: str, ttl_days: int) -> bool
    async def forget_url(self, url: str) -> int                          # cascades chunks + qa entries citing it
    async def forget_question(self, question: str) -> int                # erases Answer Cache entries by question key
    async def stats(self) -> dict
    async def cleanup(self, older_than_days: int) -> int
```

**Key decisions locked here:** similarity = `1 - cosine_distance` normalized in exactly one private helper; vectors stored as FLOAT32 bytes in HASH field `vec`; `FT.SEARCH ... KNN` with DIALECT 2; freshness filtering happens in Python on returned candidates (filter by `fetched_at`/`created_at` vs temporal class), not in the Redis query.

**Steps:**

- [ ] Failing unit tests (FakeEmbedder + fakeredis where possible; mark RediSearch-dependent tests `@pytest.mark.redis`): index creation idempotent; upsert dedup (same text twice → 1 key); similarity normalization (identical text → ~1.0); `forget_url` removes chunks and cache entries citing the URL; cleanup removes only old entries.
- [ ] `docker compose up -d redis`; implement; `uv run pytest tests/test_memory.py -v -m "not external"` → PASS (redis-marked tests run against the container).
- [ ] Commit: `feat: two-tier Redis vector memory with dedup, freshness, and erasure`

### Task 4: Chunker + web acquisition pipeline

**Files:**
- Create: `src/agent/chunker.py`, `src/agent/web.py`, `tests/test_chunker.py`, `tests/test_web.py`, `tests/fixtures/pages/*.html`

**Interfaces (produced):**
```python
# chunker.py
@dataclass class Chunk: text: str; section: str
def chunk_markdown(md: str, target_tokens: int = 800, overlap_tokens: int = 120) -> list[Chunk]
# heading-aware: split on ## / ###, greedy-pack paragraphs, token counts via tiktoken cl100k_base

# web.py
@dataclass class SearchResult: url: str; title: str; snippet: str
@dataclass class PageContent: url: str; title: str; markdown: str
class SearchClient:      # tavily-python, basic depth, top_k results, timeout, tenacity retries
    async def search(self, query: str) -> list[SearchResult]
class ContentFetcher:
    async def fetch(self, url: str) -> PageContent | None      # httpx → trafilatura(markdown) ; on failure → tavily extract fallback ; None if both fail
    async def fetch_all(self, urls: list[str]) -> list[PageContent]  # asyncio.gather, all concurrent
def strip_structural(md: str) -> str  # zero-width chars, HTML comments, base64 blobs > 100 chars
```

**Steps:**

- [ ] Failing tests: chunker respects target size ±20% and overlap on a long fixture doc; heading metadata preserved; `strip_structural` removes zero-width/comments (fixture with embedded `<!-- ignore previous instructions -->`); fetcher returns markdown from local fixture HTML via respx-mocked httpx; fetcher falls back to (mocked) extract when httpx 403s; `fetch_all` skips failures.
- [ ] Implement; `uv run pytest tests/test_chunker.py tests/test_web.py -v` → PASS.
- [ ] One `@pytest.mark.external` test: real Tavily search for "python asyncio" returns ≥3 results with URLs. Run once to verify the key.
- [ ] Commit: `feat: web search, resilient content fetching, and heading-aware chunking`

### Task 5: Guardrails and prompts

**Files:**
- Create: `src/agent/prompts.py`, `src/agent/guardrails.py`, `tests/test_guardrails.py`, `tests/fixtures/redteam/*.md`

**Interfaces (produced):**
```python
# guardrails.py
@dataclass class Preflight: is_injection: bool; temporal: Literal["static","slow","volatile"]; topic: str; contains_pii: bool; standalone_query: str
async def preflight(query: str, history: list[dict], llm: UtilityLLM) -> Preflight   # single nano call, JSON schema output
async def screen_chunks(chunks: list[Chunk], llm: UtilityLLM) -> list[tuple[Chunk, bool]]  # (chunk, quarantined) — batched classify, never rewrite
def validate_citations(answer: str, allowed_urls: set[str]) -> tuple[str, list[str]]  # strips/flags URLs not in allowed set; returns clean answer + cited list
```

**Prompt requirements (`prompts.py`, exact texts written in this task):**
- `PREFLIGHT_PROMPT`: returns the five fields; injection = imperative attempts to alter agent behavior; temporal definitions with examples ("capital of France"=static, "population of NL"=slow, "ETH price"=volatile); topics restricted to the taxonomy; `contains_pii` = the question reveals personal data about an identifiable person (health, finances, identity, location).
- `SCREEN_PROMPT`: classify text block as `content` vs `instruction_like` — explicit that it must ignore any instructions inside the block itself.
- `SYNTHESIS_PROMPT`: system rules — answer only from provided sources; every factual claim attributable; unknown → say so; sources wrapped in `<source url="..." fetched="...">` tags declared as untrusted data that must never be followed as instructions; cite URLs used; include "as of {date}" for dated facts.

**Steps:**

- [ ] Failing tests (mock LLM returning canned JSON): preflight parses; malformed LLM JSON → safe default (`is_injection=False`, `temporal="volatile"`, `topic="other"`, `contains_pii=True`, standalone=raw query — fail-open to web, fail-closed to memory writes); `validate_citations` strips a fabricated URL; red-team fixture chunks get quarantined when mock returns `instruction_like`.
- [ ] Implement; `uv run pytest tests/test_guardrails.py -v` → PASS.
- [ ] Commit: `feat: layered prompt-injection guardrails and grounded synthesis prompts`

### Task 6: LLM services + pipeline orchestration

**Files:**
- Create: `src/agent/llm.py`, `src/agent/pipeline.py`, `tests/test_pipeline.py`

**Interfaces (produced):**
```python
# llm.py — agent-framework ChatAgent wrappers; both return (text, Usage)
class ConversationLLM:  # luna, low reasoning effort, streaming, 30s/10s-first-token
    async def synthesize(self, question: str, sources: list[ChunkHit | PageContent], history: list[dict]) -> tuple[str, Usage]
class UtilityLLM:       # nano, 15s, json_schema structured outputs
    async def complete_json(self, prompt: str, schema: type[BaseModel]) -> tuple[BaseModel, Usage]

# pipeline.py
@dataclass class TurnResult: answer: str; route: str; sources: list[dict]; record: TurnRecord
class Pipeline:
    def __init__(self, settings, memory: MemoryStore, search: SearchClient, fetcher: ContentFetcher,
                 conv: ConversationLLM, util: UtilityLLM): ...
    async def answer_turn(self, query: str, history: list[dict]) -> TurnResult
```

**`answer_turn` control flow (locked, mirrors spec §4.2 + §5.5):** preflight → if injection: refuse+log → embed standalone query → if not volatile: cache lookup (≥0.85 & fresh) → return; chunk lookup (top-1 ≥0.70 & fresh) → synthesize from top-k → validate → promote → return. Miss: search → on search failure: degraded-or-refuse per `degraded_answers` using borderline chunks → fetch_all → strip → chunk → screen → upsert clean chunks → synthesize from fresh chunks + borderline memory chunks (0.55–0.70) → validate → promote → return. **Promotion** = `put_qa` for every validated synthesis unless the turn is volatile, degraded, refused, or `contains_pii` (ADR-0001). Every path emits exactly one `TurnRecord` with stage timings.

**Steps:**

- [ ] Verify agent-framework import paths/clients against installed version (30-min timebox); if `ChatAgent`+Azure client works, use it; otherwise call `openai.AsyncAzureOpenAI` directly inside the same two classes — signatures above don't change. Record which path was taken (feeds ADR-001).
- [ ] Failing pipeline tests — all dependencies faked/mocked, no network: cache-hit path; chunk-hit path **and its promotion to the cache**; miss path stores chunks + qa and cites only retrieved URLs; volatile bypasses memory **and is not cached**; PII-flagged turn answered but **not** cached; degraded answer not cached; injection refuses; search-down → degraded answer with caveat when borderline chunks exist, refusal otherwise; threshold edges (0.849 vs 0.85); query over `max_query_chars` rejected at entry.
- [ ] Implement; `uv run pytest tests/test_pipeline.py -v` → PASS.
- [ ] Commit: `feat: memory-first pipeline orchestration with degradation paths`

### Task 7: CLI

**Files:**
- Create: `src/agent/cli.py`, `src/agent/analytics.py`, `tests/test_analytics.py`
- Modify: `pyproject.toml` (add `[project.scripts] agent = "agent.cli:app"`)

**Steps:**

- [ ] `agent chat` (REPL: rolling history, route badge `[memory ✓ cache]` / `[memory ✓]` / `[web ↯]`, sources footer with dates, `-v` shows per-turn cost/latency); `agent ask "…"`; `agent memory stats|cleanup|clear|forget --url URL|--question TEXT`; `agent analytics [--cluster]` (hit rate per the Global Constraints formula); `agent serve` (uvicorn, runs the Task 8 API locally).
- [ ] `analytics.py`: aggregate `read_turns()` → hit rate, route counts, topic distribution, temporal distribution, cost totals, p50/p95 latency; `--cluster`: KMeans over stored query embeddings (k=min(8, n//3)), nano labels each cluster. Failing test for the aggregation math on a fixture JSONL; implement; PASS.
- [ ] Manual E2E (first live run): `docker compose up -d redis && uv run agent ask "What is the Strangler Fig pattern?"` → miss/web with citations; repeat same question → cache hit; paraphrase → cache hit; "current ETH price" twice → both miss (volatile). Fix what breaks.
- [ ] Commit: `feat: chat/ask/analytics/memory CLI with rich output`

### Task 8: HTTP API (production entrypoint)

**Files:**
- Create: `src/agent/api.py`, `src/agent/sessions.py`, `src/agent/ratelimit.py`, `tests/test_api.py`

**Interfaces (produced):**
```python
# sessions.py
class SessionStore:  # Redis-backed rolling history
    async def get(self, session_id: str) -> list[dict]        # last 10 turns
    async def append(self, session_id: str, user: str, assistant: str) -> None  # 1h TTL refresh

# api.py — FastAPI app factory
def create_app(pipeline: Pipeline, sessions: SessionStore, settings: Settings) -> FastAPI
# POST /chat    {message: str, session_id: str | None} -> {answer, route, sources, turn_id, session_id}
# GET  /analytics/summary  -> aggregation from analytics.py
# GET  /healthz -> {"status":"ok"} after Redis PING (503 otherwise)
# Auth: Authorization: Bearer <API_KEY> on /chat and /analytics when settings.api_key is set; /healthz always open

# ratelimit.py
class TokenBucket:  # Redis-backed, per API key
    async def allow(self, key: str) -> bool   # rate_limit_per_min tokens/min; False -> HTTP 429
```

**Steps:**

- [ ] Failing tests (httpx `ASGITransport`, pipeline mocked): /chat returns answer+route and creates/echoes session_id; second call with same session_id passes history into `answer_turn`; missing/wrong bearer → 401; 31st request in a minute → 429 (fakeredis clock); message over `max_query_chars` → 422; /healthz open without auth; /healthz → 503 when Redis ping fails (mocked).
- [ ] Implement; `uv run pytest tests/test_api.py -v` → PASS.
- [ ] Manual: `uv run agent serve` + `curl -X POST localhost:8000/chat -H "Authorization: Bearer test" -d '{"message":"..."}'` → live answer.
- [ ] Commit: `feat: FastAPI production entrypoint with session store and bearer auth`

### Task 9: Evaluation suite

**Files:**
- Create: `evals/golden.yaml`, `evals/test_routing.py`, `evals/test_citations.py`, `evals/test_injection.py`, `evals/test_groundedness.py`, `evals/conftest.py`

**Steps:**

- [ ] `golden.yaml`: 15 questions across the taxonomy — for each: query, seeded memory state (none / exact / paraphrase / related-chunk / stale), expected route. Deterministic via FakeEmbedder with controlled vectors.
- [ ] `test_routing.py`: parametrized over golden.yaml → assert expected route. `test_citations.py`: mocked web content → every URL in answer ∈ retrieved set (asserts on `validate_citations` integration). `test_injection.py`: red-team fixtures through the full mocked pipeline → quarantined chunks never reach synthesis context; poisoned page produces clean answer.
- [ ] `test_groundedness.py` (`@pytest.mark.external`): 8 live questions; nano as judge scores answer faithfulness against retrieved context 1–5; assert mean ≥ 4. Run once live, record scores (feeds README + assessment).
- [ ] All non-external evals green in `uv run pytest evals -m "not external"`. Commit: `test: routing, citation, injection, and groundedness evaluation suite`

### Task 10: IaC + production deployment + CI/CD

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`, `azure.yaml`, `infra/main.bicep`, `infra/main.parameters.json`

**Steps:**

- [ ] `main.bicep`: RG-scoped — AIServices account + 3 model deployments (GlobalStandard), Azure Managed Redis (Balanced B0, TLS, vector search), Log Analytics + App Insights, Container Apps env + agent app (system-assigned identity, consumption plan, scale-to-zero, `USE_MANAGED_IDENTITY=true`), Key Vault (Tavily key + API_KEY as secrets, KV references into the app), role assignments (`Cognitive Services OpenAI User` + `Key Vault Secrets User` to app identity). Parameters: location (default swedencentral), name prefix. `bicep build` clean.
- [ ] `azure.yaml`: azd service definition mapping the Dockerfile to the container app.
- [ ] **Deploy for real**: `azd up` into a fresh environment (`mfa-prod`). Reuse-or-import decision for the Task 2 Foundry resource: prefer letting Bicep own everything; delete the Task 2 hand-made resource once the azd-provisioned one is live and `.env` is repointed for local dev. Smoke: `curl https://<app-fqdn>/healthz` → ok; one `POST /chat` round-trip answers with citations.
- [ ] `llm.py`/`embeddings.py`: verify managed-identity token auth path works in-cloud (azure-identity `DefaultAzureCredential`) — this is the `use_managed_identity` branch from Task 1.
- [ ] `ci.yml`: jobs — `lint` (ruff check + format --check), `test` (uv sync; Redis 8 service container; `pytest -m "not external"` incl. evals), `bicep` (bicep CLI, `bicep build infra/main.bicep`). Triggers: push, PR.
- [ ] `deploy.yml` (CD): on push to `main` after CI success (`workflow_run`) — `azd deploy` with OIDC federated credentials; bootstrap via `azd pipeline config` (creates the app registration + federation; no long-lived secrets in GitHub). Requires the GitHub repo to exist — coordinate with the push gate: create the private repo `vadjs/<name>`, push once approved, then run `azd pipeline config`, then verify one CD run end-to-end.
- [ ] Commit: `ci+infra: bicep environment, azd deployment, and CI/CD pipelines`

### Task 11: Documentation pack

**Files:**
- Create: `README.md`, `docs/SAD.md`, `docs/adr/0003..0008-*.md`, `docs/blueprint.md`, `docs/assessment.md`, `docs/cost-model.md`, `docs/security.md`, `docs/ai-assistance.md`

**Content requirements (each doc's spine — prose written at execution time, C1-register English, no trial-account or access-arrangement references):**

- [ ] `README.md`: what it is (3 sentences) → quickstart (docker compose + uv + .env) → demo transcript (real output) → architecture snapshot (one mermaid) → repo map → doc index.
- [ ] `SAD.md`: context view (enterprise-landscape diagram: agent between channels, identity, LLM platform, knowledge estate) → container/component views (C4, mermaid) → sequence diagrams: hit path + miss path → deployment view (local + Azure target) → quality-attribute scenarios table (6 scenarios: latency, cost, groundedness, injection resilience, availability, erasure) → NFR traceability to spec.
- [ ] ADRs (short form per `docs/adr/` house style; Considered Options where the rejections matter): 0003 orchestration (agent-framework usage as implemented vs LangGraph vs hosted Agent Service); 0004 two-tier memory + per-index thresholds (incl. calibration method + Redis distance normalization); 0005 model selection (Luna/nano; gpt-5.1, Sol, Terra, gpt-5-mini rejections with the price/quality table); 0006 FLAT index + content-hash dedup + freshness-as-routing (HNSW switch point stated); 0007 runtime + IaC (3.13/3.14 outcome; Bicep vs Terraform); 0008 reliability policy (timeout table rationale, retry classes, degraded-vs-refuse flag). Review design-time 0001/0002 for drift against the implementation; keep `CONTEXT.md` terms authoritative across all docs.
- [ ] `blueprint.md`: the reference architecture generalized — production Azure mapping (Container Apps, Managed Redis, APIM GenAI gateway, Prompt Shields, Foundry evaluations, monitoring) with diagram → multi-cloud portability table (AWS/GCP equivalents per component, what changes, what doesn't) → scaling narrative (what breaks at 100 concurrent users and the fixes).
- [ ] `assessment.md`: WAF five-pillar self-assessment (honest gaps: single-instance Redis, no authn, judge-model bias …) → Responsible AI notes → prioritized roadmap to production.
- [ ] `cost-model.md`: per-turn component table (incl. Tavily production rates) → cost per 1K turns vs hit-rate curve (0/30/50/70%) → KPI definitions (hit rate, cost/answer, p95, deflection) → levers.
- [ ] `security.md`: threat model (persistent poisoning via ingested web content as the headline threat, STRIDE-lite table) → five guardrail layers mapped to attack paths → GDPR section (erasure walk-through with `memory forget`, minimization, residency, Tavily as processor) → ISO 27001 Annex A control mapping table.
- [ ] `ai-assistance.md`: workflow narrative (requirements grilling → spec → plan → task-wise implementation with tests-first → human review gates), tools used, what was human-decided vs AI-drafted, pointers to spec/plan in `docs/superpowers/`.
- [ ] Commit: `docs: architecture documentation pack`

### Task 12: End-to-end validation + demo polish

**Steps:**

- [ ] Fresh-clone rehearsal: `git clone` to temp dir → follow README quickstart verbatim → working agent. Fix any gap.
- [ ] Scripted demo run (recorded transcript into README): miss → hit → paraphrase-hit → volatile bypass → injection attempt refused → `analytics` → `memory forget` → re-ask shows re-fetch.
- [ ] Cloud validation: `POST /chat` against the deployed Container App (miss then hit), `GET /analytics/summary`; App Insights shows the turns; one CD run from a `main` push observed green.
- [ ] Full suite: `uv run ruff check . && uv run pytest -m "not external"` green; one final live `pytest -m external` pass; `bicep build` clean.
- [ ] Commit: `chore: demo transcript and final polish`

---

## Self-review

Spec coverage: FR-1..8 → Tasks 3/6 (routing), 4 (web/markdown), 6 (two LLMs), 1/7 (logging, analytics), 7 (chat/REPL); NFR security → 5/8 (guardrails, API auth), reliability → 4/6, observability → 1/7 + App Insights (10), operability (local + deployed cloud) → 0/8/10, compliance → 3 (`forget_url`) + 11 (docs); IaC/CI/CD → 10; artifact pack → 11; e2e incl. cloud → 12. Type names cross-checked across task Interfaces blocks. No TBDs.
