# Timeouts bound failure detection; retries are for transport, not quality

Every timeout is sized ~3× the dependency's typical p99 and tuned from measured turns
(search 5s vs ~1–2s typical; per-page fetch 10s, all pages concurrent; embeddings 10s;
utility LLM 15s vs ~1–2s; conversation 30s vs ~3–5s; Redis 2s). Retries (tenacity,
exponential backoff + jitter, one decorator module) apply only to idempotent or
transport-class failures: search, fetch, embeddings, and Redis reads/writes (idempotent
via content-hash keys), plus LLM calls on 429/5xx/timeout — never on "bad output", which
is the evaluation suite's territory, not the retry loop's.

Degradation ladder, most value first: per-page fetch failures skip the page, never the
turn; web search down → serve the best below-threshold memory explicitly labeled stale
(`DEGRADED_ANSWERS=true`, config-switchable to strict refusal), refuse only with nothing
relevant; model-quota exhaustion (429 storms) → bounded backoff then an explicit
"at capacity" refusal — never silent quality degradation. Degraded answers are never
cached (ADR-0001): a caveat must not launder into a confident future answer.
