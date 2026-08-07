# Cost Model

All prices are Azure list rates (per 1M tokens: gpt-5.6-luna $0.20/$1.20, gpt-5-nano
$0.05/$0.40, text-embedding-3-small $0.02) and Tavily production pay-as-you-go
($0.008/credit; basic search = 1 credit). Measured token counts come from live turn
telemetry (`logs/turns.jsonl`).

## Per-turn economics (measured)

| Component | Miss (web) | Chunk hit | Cache hit |
|---|---|---|---|
| Tavily search (1 credit) | $0.0080 | — | — |
| Tavily Extract fallback (~0.2 cr/page, occasional) | ~$0.0-0.0016 | — | — |
| Preflight + ingest screens (nano, ~4-8K tok) | ~$0.0004 | ~$0.0001 | ~$0.0001 |
| Synthesis (Luna, ~2.5-5K in / 200-500 out) | ~$0.0009 | ~$0.0009 | — |
| Embeddings (query + chunks) | ~$0.0001 | ~$0.00002 | ~$0.00002 |
| **Total** | **≈ $0.010** | **≈ $0.001** | **≈ $0.0001** |

Two facts with architectural consequences:

1. **The search API dominates the miss path** (~80% of turn cost) — not the LLM. Every
   memory hit avoids the single most expensive and slowest component entirely.
2. The spread between a miss and a cache hit is **~100×**; between a miss and a chunk
   hit ~10×. Memory hit rate is therefore the system's dominant cost lever.

## Cost per 1,000 turns vs hit rate

Assuming hits split evenly between tiers:

| Memory hit rate | Cost / 1K turns | vs no-memory baseline |
|---|---|---|
| 0% (plain web agent) | $10.00 | — |
| 30% | $7.17 | −28% |
| 50% | $5.28 | −47% |
| 70% | $3.38 | −66% |

The same curve governs **capacity**: token demand against Azure OpenAI quota scales with
(1 − hit rate), so cache growth raises the sustainable QPS ceiling at fixed quota
(spec §3.1) — hit rate is simultaneously the cost lever and the capacity lever.

## Fixed costs (production reference environment)

| Resource | Monthly |
|---|---|
| Azure Managed Redis Balanced B0 | ~$16 |
| Container Apps (consumption, scale-to-zero) | ~$0 idle; cents/day active |
| Log Analytics / App Insights (30-day retention, low volume) | ~$1–3 |
| ACR Basic | ~$5 |
| Model deployments (Global Standard) | $0 idle — per-token only |

## KPIs to run this by

- **Memory hit rate** (`analytics`): the business metric; each point of hit rate is
  directly convertible to $/1K turns and to quota headroom.
- **Cost per answered turn**: total spend / answered turns; alarms on regression.
- **p50/p95 latency per route**: hit ≈ 1–3s, miss ≈ 8–15s measured; degradation visible
  per stage in turn telemetry.

## Levers, in order of power

1. Grow hit rate (better standalone rewrites, corpus curation, threshold calibration).
2. Trim synthesis context (chunk budget currently 12; each 1K input tokens ≈ $0.0002).
3. Search depth (basic vs advanced doubles Tavily cost per miss).
4. Model tier (Terra upgrade multiplies synthesis cost 10×; justify with evals first).
