# Pages are summarized at ingest, after screening

FR-3/FR-5 call for summarization on the web path, and retrieval needs a page-level target: 800-token chunks embed local detail, so broad questions ("what is X, roughly?") can land below the chunk gate even when the Knowledge Base holds the right page. We decided the utility model condenses each fetched page — after the ingest screen, from clean chunks only — into a **Page Summary** stored in the Knowledge Base with the page's provenance and placed first in that page's synthesis context. Summarization rewrites text, which the sanitizer paradox (spec §8) forbids for untrusted content; the boundary holds because the summarizer's input is already screened, and its output is checked for injection markers and dropped — never repaired — on a match. A failed or dropped summary degrades to "no summary" and never fails the turn.

## Considered Options

- **No summarization; raw chunks only (previous state)** — rejected: leaves FR-3/FR-5 unimplemented, and broad queries miss pages the Knowledge Base already holds because no page-level embedding exists.
- **Summarize the raw page before screening** — rejected: the summarizer would rewrite unscreened content — exactly the sanitizer paradox; a poisoned page could steer its own summary, which then persists as clean-looking memory.
- **Map-reduce synthesis over per-page summaries instead of chunks** — rejected: answers would inherit summary-stage information loss on every turn, and citations would ground claims in derived text rather than fetched text.
- **Summarize clean chunks after screening (chosen)** — one nano call per page (~$0.0003 per miss, parallel across pages), a page-level retrieval target, and the classify-never-rewrite core preserved.
