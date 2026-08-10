# Python 3.14 on uv; Bicep + azd for infrastructure

**Python 3.14**: the rule applied was "latest release line the full dependency chain
verifiably supports" — resolution and the complete test suite passed on 3.14 at scaffold
time (agent-framework, trafilatura/lxml, redis-py, tiktoken all ship 3.14 wheels), so the
newest line won. Had any wheel been missing, 3.13 was the fallback; an I/O-bound LLM
workload gains nothing from 3.14's headline features, so the version choice is about
support surface, not performance.

**Bicep + azd, not Terraform**: the estate is Azure-only, so Azure-native IaC wins —
no state backend to manage (ARM is the source of truth), day-0 support for the newest
resource types (Foundry model deployments, Managed Redis), and first-class azd
integration. `azure.yaml` declares the Foundry project and the hosted agent as azd
services with an `infra:` block pointing at `infra/` Bicep, so one tool owns both
halves: `azd provision` runs the Bicep, `azd deploy` (the `azure.ai.agents` extension)
ships the agent as a code zip with remote build. CI validates with `bicep build`; CD is
OIDC-federated with no long-lived cloud secrets in GitHub (ADR-0009).

## Considered Options

- **Terraform** — the right choice when the estate spans clouds or an organization
  already standardizes on it (state management, policy-as-code ecosystem, one language
  everywhere). For a single-cloud reference implementation it adds a state backend and a
  provider-lag risk without adding capability.
