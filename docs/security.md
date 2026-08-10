# Security Design

## Threat model

The defining property of a memory-first agent is that **retrieved web content becomes
persistent state**. That changes the primary threat from "a user types a jailbreak" to
**persistent memory poisoning**: instructions embedded in a fetched page get summarized,
chunked, stored — and would then re-serve themselves in every future related turn, to
every user. The design treats memory as a trust boundary.

| # | Threat (STRIDE) | Vector | Mitigations |
|---|---|---|---|
| T1 | Tampering — direct prompt injection | User input alters agent behavior | Deterministic pattern screen (zero-latency layer 1a) + LLM classification in preflight (1b); refusal template |
| T2 | Tampering — indirect injection / memory poisoning | Instructions inside fetched pages | Structural stripping (hidden text, zero-width chars, HTML comments, data blobs); ingest screening: classify-and-quarantine, never rewrite; spotlighted `<source>` framing at synthesis |
| T3 | Spoofing — fabricated citations | Model invents URLs, lending false authority | Output validation: citations must be a subset of actually-retrieved URLs; violators stripped |
| T4 | Information disclosure — PII in shared memory | Cached user questions served cross-session | PII gate in preflight (fail-closed) blocks Answer Cache writes; `forget --question` erasure (ADR-0001) |
| T5 | Denial of service / cost attack | Leaked API key drives search + token spend | Bearer auth; per-key rate limit (429); input-size cap; scale-to-zero blast-radius |
| T6 | Elevation — retrieved content triggering actions | Content-initiated tool use | Structural: the agent has no side-effecting tools; the maximum blast radius of a successful injection is a wrong answer, which T3 controls and the evaluation suite target |

## The five guardrail layers

1. **Input screen** — regex pre-check for canonical injection markers (deterministic,
   free), then utility-model classification. The layered order matters: the regex is not
   bypassable by classifier weakness, and the classifier catches what patterns cannot.
2. **Ingest screen** — markdown conversion drops scripts; explicit stripping of
   zero-width characters, HTML comments, and base64 blobs; then chunk-level
   classification. Flagged chunks are **quarantined**: stored with their verdict for
   audit, excluded from all retrieval. Classification-not-rewriting is deliberate — a
   sanitizer that rewrites poisoned text can itself be injected.
3. **Prompt architecture** — instruction hierarchy plus spotlighting: retrieved content
   arrives inside `<source url="…">` tags declared as untrusted data. Hierarchy
   fine-tuning reduces injection success; it is treated as one layer, never the control.
4. **Output validation** — the citation-subset invariant, enforced in code.
5. **Least privilege** — no tools, no shell, no outbound actions from model output;
   admin verbs (memory inspection, erasure) exist only in the CLI, never over HTTP.

Verified end-to-end by the red-team evals (`evals/test_injection.py`): a poisoned page's
clean sections survive ingestion while the injected section is quarantined, and the
poison does not surface in subsequent memory-served turns.

## Platform security (production reference)

The agent runs in per-session VM-isolated sandboxes with a dedicated Entra Agent ID
(ADR-0009). Key Vault is the secret source of truth — the generated Redis URL, the
model key, and the Tavily key are written there by IaC and read back only at deploy
time into the agent version's environment; TLS everywhere (Managed Redis on
10000/TLS). No secret exists in the repo or the code zip, and GitHub holds only the
Tavily seed (CD authenticates via OIDC federation). Hardening path: runtime Key Vault
resolution and Entra-based model auth via the Agent ID once its RBAC can pre-exist
deploy.

## GDPR

Personal data enters through two doors: user questions and ingested web content.

- **Minimization**: no accounts or user identity; turns log an opaque `turn_id`;
  sessions expire after 1h.
- **The PII gate** keeps personal questions out of shared memory entirely (ADR-0001).
- **Erasure by provenance**: `agent memory forget --url <URL>` cascades through both
  memory tiers via stored provenance; `--question "<text>"` erases a cache entry by key.
  Erasure in vector stores is a known hard problem; provenance metadata makes it a
  targeted delete instead of a reindex.
- **Residency**: the reference deployment pins `swedencentral`; Azure OpenAI does not
  train on customer data (Microsoft DPA applies).
- **Processor transparency**: search queries transit Tavily as a third-party processor —
  a production deployment should cover it in the controller's processor register; the
  volatile-class bypass means queries never persist in Tavily-derived memory anyway.

## ISO 27001 alignment

ISO 27001 certifies an organization's ISMS, not software. The solution is designed to
operate within a certified ISMS and maps to Annex A controls:

| Annex A domain | Implementation |
|---|---|
| Cryptography | TLS in transit (HTTPS ingress, Redis TLS); Azure encryption at rest |
| Access control | RBAC via managed identity; Key Vault; bearer auth + rate limiting on the API; admin verbs CLI-only |
| Logging & monitoring | Per-turn structured telemetry; Application Insights; health probes |
| Secure development | CI quality gates (lint, tests, evals, IaC validation); locked dependencies; reviewed changes |
| Supplier relationships | Microsoft, OpenAI, and Tavily each hold ISO 27001 certification |
| Operations security | IaC-defined environments; reproducible builds; scale-to-zero limits standing exposure |
