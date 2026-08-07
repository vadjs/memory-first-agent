"""Citation-validity evals: answers may only cite actually-retrieved URLs (spec §8)."""

import pytest

from evals.conftest import PAGE_URL, QUERY, EvalConv

pytestmark = pytest.mark.redis


async def test_fabricated_source_stripped_and_not_cited(eval_env):
    conv = EvalConv(answer=f"Claim. Sources: {PAGE_URL} https://fabricated.test/nope")
    pipeline, _ = await eval_env(conv=conv)
    result = await pipeline.answer_turn(QUERY)
    assert result.record.cited_urls == [PAGE_URL]
    assert "fabricated.test" not in result.answer


async def test_citations_subset_invariant_on_miss_path(eval_env):
    pipeline, _ = await eval_env()
    result = await pipeline.answer_turn(QUERY)
    assert set(result.record.cited_urls) <= {PAGE_URL}


async def test_cache_hit_reserves_original_sources(eval_env):
    pipeline, _ = await eval_env(seed="cache_exact")
    result = await pipeline.answer_turn(QUERY)
    assert result.route == "hit_cache"
    assert result.record.cited_urls == ["https://kb.test/a"]
