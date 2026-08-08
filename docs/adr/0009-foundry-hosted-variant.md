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
