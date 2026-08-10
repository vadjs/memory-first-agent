# A Foundry Hosted Agent variant, same pipeline, second deployment target

Since Build 2026, Foundry Agent Service hosts *code-first* agents: you bring your own
container or source (any framework), and the platform supplies per-session sandbox
isolation, an Entra Agent ID, built-in OpenTelemetry tracing, and fleet-scale agent
operations — the governance surface large enterprises need. We decided to add a second
deployment target on this branch: the identical `Pipeline` exposed through the
agent-framework chat-client protocol (`agent/hosted.py`) and served by
`ResponsesHostServer` (`foundry/main.py`), deployed as a code zip with remote build —
no Dockerfile involved. Memory stays the shared Azure Managed Redis: hosted sessions
carry conversation history in the Responses protocol itself, so the hosted variant
needs no session store of its own.

## Considered Options

- **Container Apps only (main branch)** — full control of the HTTP contract
  (`/analytics`, `/healthz`, bearer + rate limiting), GA runtime, whole-estate IaC.
  Remains the primary deployment; the custom API surface has no hosted equivalent.
- **Hosted Agent only** — rejected: the analytics endpoint and admin separation would
  still need a home, and parts of the hosting platform are preview.
- **Both (chosen)** — the pipeline is deployment-agnostic by construction; maintaining
  two thin entrypoints costs ~150 lines and demonstrates the actual trade-off:
  self-hosted control versus platform-managed agent operations.

## Consequences

- Hosted-variant configuration uses platform environment variables, including secrets —
  coarser than the Container Apps Key Vault references; hardening path is Entra Agent
  ID RBAC for model access and platform secret references as they mature.
- The Answer Cache is shared *across deployment targets*: a question answered through
  the Container Apps API becomes a cache hit for the hosted agent, and vice versa —
  memory is the system of reuse, not the runtime.
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
