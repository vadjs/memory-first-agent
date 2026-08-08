# Code-first orchestration on MS Agent Framework; routing logic stays plain Python

The memory-first routing *is* the product's intelligence, so it lives as a plain-async
pipeline (`agent/pipeline.py`) that any engineer can read top-to-bottom, while MS Agent
Framework provides the model layer: the conversation role runs as an `Agent` over
`OpenAIChatClient`, which speaks the Responses API against the Azure endpoint (reasoning
effort pinned to `none` for chat-grade latency). The utility role uses the OpenAI SDK's
typed `chat.completions.parse`, because the guardrails depend on schema-enforced JSON.

## Considered Options

- **LangGraph** — mature graph orchestration, but adds a second framework vocabulary for
  a graph that is a straight line with one branch; rejected as accidental complexity here.
- **Foundry Agent Service (hosted agents)** — since Build 2026 the hosted runtime is
  code-first too (bring your own container or source, any framework), so it does *not*
  take the orchestration away; the remaining trade-offs are protocol shape (Responses
  surface vs our custom API), per-session state philosophy vs our shared memory, and
  preview maturity. Implemented as a second deployment target on the `foundry-hosted`
  branch (ADR-0009 there); Container Apps remains primary for the custom HTTP contract
  and GA runtime.
- **Agent Framework Workflows** — the framework's own graph layer; unnecessary for one
  linear flow, and keeping components framework-independent preserved testability
  (91 tests run without any framework import in the routing path).
