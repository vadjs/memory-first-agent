# Foundry Hosted Agent is the sole cloud deployment target, deployed azd-natively

Since Build 2026, Foundry Agent Service hosts *code-first* agents: you bring your own
container or source (any framework), and the platform supplies per-session sandbox
isolation, an Entra Agent ID, built-in OpenTelemetry tracing, and fleet-scale agent
operations — the governance surface large enterprises need. We decided to make it the
**only** cloud deployment target: the `Pipeline` exposed through the agent-framework
chat-client protocol (`agent/hosted.py`) and served by `ResponsesHostServer`
(`src/main.py`), deployed as a code zip with remote build — no Dockerfile involved.
Deployment is azd-native: the agent is declared as an `azure.ai.agent` service in
`azure.yaml` (`codeConfiguration`: `python_3_14`, remote build) and `azd deploy`
packages `src/`, uploads, polls for `active`, and wires the platform-created agent
identity's RBAC — Microsoft's documented recommended path, replacing the hand-rolled
SDK deploy script this branch started with. Memory stays the shared Azure Managed
Redis; hosted sessions carry conversation history in the Responses protocol itself, so
the hosted variant needs no session store of its own. The FastAPI/CLI surface remains
as a **local development surface** (analytics, memory admin, evals against local
Redis) — it is application code, not a deployment target.

## Considered Options

- **Container Apps only** — full control of the HTTP contract (`/analytics`,
  `/healthz`, bearer + rate limiting), GA runtime, whole-estate IaC. Rejected as a
  cloud target: it duplicates what the platform now operates for us (sessions,
  identity, tracing, versioned rollout), and the custom API surface it existed to
  serve is a dev/admin tool, not a product endpoint.
- **Both targets** — the initial decision on this branch: the pipeline is
  deployment-agnostic by construction, so a second target cost ~150 lines and
  demonstrated the trade-off. Retired: two cloud targets mean two secret paths, two
  RBAC models, and double the operational surface for a system with one consumer;
  the demonstration had served its purpose.
- **Hosted Agent only (chosen)** — one estate to provision (`infra/` Bicep), one
  deploy mechanism (`azd deploy`), platform-managed agent operations. Cost: parts of
  the hosting platform and the `azure.ai.agents` azd extension are preview.

## Consequences

- Hosted-variant configuration arrives as environment variables on the immutable
  agent version. **Key Vault is the secret source of truth**: Bicep writes the
  generated Redis URL, the model key, and the Tavily seed into the vault; the CD
  pipeline reads them back at deploy time into the version's env map, so GitHub holds
  only the Tavily seed and OIDC bootstrap. Hardening path: runtime Key Vault
  resolution by the Entra Agent ID (blocked today: the identity is created at first
  deploy, so its RBAC cannot pre-exist in IaC) and Entra-based model auth.
- The Foundry portal's operational surface is wired in: an `AppInsights` project
  connection (IaC) feeds **Traces** and **Monitor** from the same Application Insights
  the rest of the estate uses — one observability plane, two consoles — while the agent
  exports OpenTelemetry gen_ai spans (`invoke_agent` → model/embedding child spans) via
  `configure_observability` + `enable_instrumentation`. **Evaluation** runs are
  submitted with `scripts/run_foundry_eval.py` in two kinds mirroring the offline/online
  split: project-level **dataset evals** (Groundedness + Relevance over the golden set)
  and agent-scoped **trace evals** (`--traces`: Foundry judges the agent's real
  conversations pulled from its own telemetry — the runs the agent's Evaluation tab
  lists). Trace evals required three wirings, each an honest prerequisite: the platform
  agent GUID (not the name) as the trace filter; App Insights/Log Analytics read roles
  for the *project's* managed identity (IaC); and conversation-content capture in spans
  (`CAPTURE_TRACE_CONTENT`, default on here) — content-in-telemetry is a data-governance
  decision, so it is a switch, not an assumption.
