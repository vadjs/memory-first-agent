# How AI Assistance Was Used

The task invites AI assistance and asks for a full account. This project used
Claude (Claude Code) as an engineering assistant throughout, under a deliberately
gated, spec-first workflow. The short version: **AI drafted, a human decided** — every
architectural decision passed through explicit review gates before any code existed.

## The workflow

1. **Requirements interrogation.** Before any design, the assistant ran a structured
   "grilling" session against the task: ~16 challenge questions on retrieval semantics
   (semantic cache vs RAG memory), threshold calibration, memory freshness, injection
   threat models, cost justification, and reliability policy. Weak initial positions —
   for example, "raise the similarity threshold to 0.85" or "instruction-hierarchy
   fine-tuning is sufficient protection" — were challenged with evidence and corrected
   before they could become design.
2. **Specification, reviewed twice.** The design spec
   (`docs/superpowers/specs/2026-08-07-memory-first-agent-design.md`) was drafted by the
   assistant, then reviewed line-by-line by the author across multiple rounds (model
   selection was re-decided against fresher market data during one of them; a System
   Design Interview framework audit added the scale envelope, rate limiting, durability
   and consistency stances during another). Domain decisions from these sessions were
   recorded as ADRs 0001–0002 and the glossary (`CONTEXT.md`) at the moment they settled.
3. **Plan, reviewed.** The implementation plan
   (`docs/superpowers/plans/2026-08-07-memory-first-agent-plan.md`) locked interfaces,
   test-first steps, and commit boundaries per task, and was approved before execution.
4. **Implementation, test-first, committed per task.** Each task wrote failing tests
   first, implemented to green, and committed only at task completion. The assistant
   executed; the plan (human-approved) constrained what it could decide alone.
5. **Live verification.** Every claim in these documents that reads "measured" comes
   from actual runs: the routing behavior, latencies, per-turn costs, and the
   groundedness score (4.12/5 across the golden set) are telemetry, not estimates.

## What the AI contributed vs what the human decided

| | |
|---|---|
| AI-drafted | Code, tests, documentation prose, diagrams, research summaries (model pricing/benchmarks, API changes) |
| Human-decided | Target architecture and every gated decision: framework choice, model selection, memory content policy (PII gate, promotion), thresholds, Bicep-vs-Terraform, degradation semantics, scope of the deployment |
| AI-caught | Cost-model gap (search API dominates miss cost), asymmetric-retrieval threshold trap, sanitizer paradox, preflight classifier miss on canonical injections (fixed with a deterministic pre-screen) |
| Human-caught | Framework/tooling contradictions in drafts, fetch-concurrency over-caution, missing production deployment requirement, stale model choices |

## Why this is itself an engineering practice

The gates exist because assistant output is fluent enough to *look* finished before it
*is* finished. Forcing every design through interrogation → written spec → written plan →
test-first execution converts AI speed into reviewable artifacts instead of unreviewable
velocity. The commit history reflects task boundaries; the spec and plan in
`docs/superpowers/` are the audit trail of what was decided when, and by which party.
