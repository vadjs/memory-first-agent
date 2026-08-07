# gpt-5.6-luna for conversation, gpt-5-nano for utility, text-embedding-3-small

| Role | Model | $/1M in/out | Rationale |
|---|---|---|---|
| Conversation | `gpt-5.6-luna` | 0.20 / 1.20 | Newest generation (Jul 2026), ~190 tok/s, strong knowledge/low-hallucination benchmark profile, 1M context; run at reasoning effort `none` — grounded synthesis needs no chain-of-thought |
| Utility | `gpt-5-nano` | 0.05 / 0.40 | Preflight classification, injection/ingest screening, topic tagging, eval judging: structurally simple calls on the critical path of every turn; smallest time-to-first-token wins |
| Embeddings | `text-embedding-3-small` | 0.02 / — | 1536 dims; `-large` costs ~6.5× for marginal recall gain at this corpus size |

## Considered Options

- **`gpt-5.1`** ($1.25/$10) — dominated by Luna on price (~6–8×), speed, and recency at
  comparable quality after Luna's July 2026 price reduction.
- **`gpt-5.6-sol`** ($5/$30) — frontier reasoning model for long-horizon agentic work;
  this system's intelligence lives in the routing pipeline, not chain-of-thought, so Sol
  buys unused depth at 25× the output price.
- **`gpt-5.6-terra`** ($2/$12) — the documented quality-upgrade path; 10× Luna's price
  for marginal gain on grounded synthesis.
- **`gpt-5-mini`** ($0.25/$2) — costs *more* than Luna, eliminating it from both roles.

Model bindings are deployment-name environment config; swapping a model or the provider
(Azure ↔ OpenAI direct) requires no code change.
