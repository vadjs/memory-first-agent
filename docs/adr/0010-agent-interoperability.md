# The agent joins multi-agent estates as a callee: Responses natively, A2A via the platform bridge

This agent is a *specialist*: grounded memory-first Q&A behind one narrow contract. In a
multi-agent estate it is therefore a **callee** — supervisors, workflows, and peer agents
delegate questions to it — and it exposes two doors over the same implementation:

- **Responses (native)**: the hosted agent's OpenAI-compatible endpoint
  (`…/agents/memory-first-agent/endpoint/protocols/openai/responses`). Any orchestrator
  holding an Entra token with **Foundry Agent Consumer** on the project can call it with
  a stock OpenAI SDK — this is how the eval harness already invokes it.
- **A2A (platform bridge)**: Foundry bridges the Agent2Agent protocol onto any endpoint
  that implements responses. `scripts/enable_a2a.py` publishes the agent card (skill:
  `memory-first-qa`) and enables `a2a` on the endpoint; the card is then discoverable at
  `…/protocols/a2a/agentCard/v1.0`. Other Foundry agents wire it as an A2A tool
  (`RemoteA2A` connection, `AgenticIdentityToken` auth — the caller's own Entra Agent
  ID), Foundry **Workflows** can orchestrate it declaratively, and non-Foundry agents
  use any A2A SDK with an Entra bearer. `azd deploy` resets endpoint protocol
  configuration, so CD re-applies the bridge after every deploy (idempotent PATCH).

Outgoing delegation is **deliberately absent**: the pipeline is tool-free by design
(least privilege, spec §8 layer 5) — a memory-first answerer has no reason to call
other agents, and every outbound channel is injection attack surface. If a future
skill genuinely requires delegation, the platform path is a Toolbox-wrapped A2A tool
consumed through the project's MCP endpoint — documented here, not implemented.

## Considered Options

- **Serve A2A natively in-container** (declare `a2a` in the version's protocol list and
  run a second protocol server) — rejected: the bridge provides A2A *for free* on top
  of responses, and preview A2A is text-only without streaming, so in-container serving
  adds surface without adding capability.
- **Custom A2A wrapper / Control Plane registration** — the documented route for agents
  hosted *outside* Agent Service; unnecessary for a hosted agent.
- **Classic Connected Agents** — retired by the platform; its replacements (A2A tool,
  Workflows) are exactly the two consumption paths above.
- **Outgoing A2A/toolbox now** — rejected as speculative capability that would breach
  the tool-free guardrail posture.

## Consequences

- Caller authorization is per-identity RBAC (**Foundry Agent Consumer** on the project),
  granted when a real caller appears — deliberately not pre-provisioned in IaC, since
  the caller set is unknown and least privilege wins.
- A2A preview limits apply: text modality only, no streaming, JSONRPC transport for
  v1.0, Entra-only auth (no anonymous card discovery).
- The Answer Cache compounds across orchestrators: a question any supervisor delegates
  becomes a memory hit for every later caller — the shared-memory economics (ADR-0009)
  extend to the multi-agent estate unchanged.
