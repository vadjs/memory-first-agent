# Two memory tiers with per-index thresholds; Redis distance normalized in one place

A question↔question match (Answer Cache) is symmetric — paraphrases embed close — while a
question↔chunk match (Knowledge Base) is asymmetric, with systematically lower cosine
similarity. One threshold cannot serve both, so the tiers gate independently: cache at
0.85, chunks at the task-default 0.70 (deliberately conservative — a redundant search is
cheaper than a misleading answer), with 0.55–0.70 treated as borderline context that
augments web synthesis. Redis returns cosine *distance*; exactly one helper
(`to_similarity`) converts it, eliminating the classic off-by-direction bug.

## Consequences

- Thresholds are per-embedding-model artifacts, not universal constants: swapping
  `text-embedding-3-small` requires recalibration on a small labeled set (measure
  relevant vs irrelevant similarity distributions, place the cutoff for target precision).
- Observed live: an exact repeat hits the cache; a paraphrase typically lands below 0.85
  and is served by the chunk tier instead — both routes are memory hits, which is the
  intended division of labor, not a defect.
