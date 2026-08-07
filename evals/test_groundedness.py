"""Live groundedness evals (LLM-as-judge, utility model). Runs only with real keys:
    uv run pytest evals/test_groundedness.py -m external
Judges whether answers are faithful to their retrieved context (spec §11)."""

import pytest
from pydantic import BaseModel

from agent.config import Settings
from agent.embeddings import AzureEmbedder
from agent.llm import ConversationLLM, UtilityLLM
from agent.memory import MemoryStore
from agent.pipeline import Pipeline
from agent.web import ContentFetcher, SearchClient

pytestmark = pytest.mark.external

GOLDEN_QUESTIONS = [
    "What is the strangler fig pattern in software architecture?",
    "What does the CAP theorem say about distributed systems?",
    "What is Redis and what is it commonly used for?",
    "How does HNSW approximate nearest neighbor search work?",
    "What is prompt injection in AI security?",
    "What is the difference between Docker and a virtual machine?",
    "What is infrastructure as code?",
    "What is retrieval-augmented generation (RAG)?",
]

JUDGE_SYSTEM = """You judge whether an ANSWER is grounded in its CONTEXT.
Score 1-5: 5 = every claim supported by the context; 3 = mostly supported with
minor unsupported details; 1 = substantially unsupported or contradicted.
Judge only groundedness, not style or completeness. Return JSON."""


class Verdict(BaseModel):
    score: int
    reason: str


@pytest.fixture(scope="module")
def live():
    settings = Settings()
    if not settings.azure_openai_api_key or not settings.tavily_api_key:
        pytest.skip("live credentials not configured")
    memory = MemoryStore(settings.redis_url, AzureEmbedder(settings), namespace="mfaground")
    util = UtilityLLM(settings)
    pipeline = Pipeline(
        settings,
        memory,
        SearchClient(settings),
        ContentFetcher(settings),
        ConversationLLM(settings),
        util,
    )
    return pipeline, memory, util


async def test_groundedness_mean_at_least_4(live):
    pipeline, memory, util = live
    await memory.clear()
    await memory.ensure_indexes()
    scores = []
    for question in GOLDEN_QUESTIONS:
        result = await pipeline.answer_turn(question)
        assert result.route in ("miss_web", "hit_chunks"), f"{question}: {result.route}"
        context_hits = await memory.search_chunks(question, k=5)
        context = "\n\n".join(h.text for h in context_hits) or "(no context)"
        verdict, _ = await util.complete_json(
            JUDGE_SYSTEM,
            f"CONTEXT:\n{context}\n\nANSWER:\n{result.answer}",
            Verdict,
        )
        scores.append(verdict.score)
        print(f"  [{verdict.score}/5] {question} — {verdict.reason[:80]}")
    mean = sum(scores) / len(scores)
    print(f"groundedness mean: {mean:.2f} over {len(scores)} questions")
    assert mean >= 4.0
    await memory.clear()
