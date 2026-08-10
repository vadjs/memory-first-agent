"""The hosted-agent adapter: Pipeline exposed through the agent-framework protocol
so Foundry's Responses host can run it (ADR-0009)."""

from agent_framework import Agent, Message

from agent.hosted import PipelineChatClient, build_hosted_agent
from agent.pipeline import TurnResult
from agent.telemetry import TurnRecord


def record(route="miss_web") -> TurnRecord:
    return TurnRecord(
        turn_id="t-1",
        query="q",
        route=route,
        topic="technology",
        temporal="static",
        injection_flagged=False,
        contains_pii=False,
        scores={},
        stages=[],
        usages=[],
        total_cost_usd=0.001,
        cited_urls=["https://a.com/doc"],
    )


class FakePipeline:
    def __init__(self):
        self.calls: list[tuple[str, list[dict]]] = []

    async def answer_turn(self, query, history=None, session_id=""):
        self.calls.append((query, list(history or [])))
        return TurnResult("The answer.", "hit_chunks", [{"url": "https://a.com/doc"}], record())


async def test_agent_run_returns_answer_with_sources():
    pipeline = FakePipeline()
    agent = build_hosted_agent(PipelineChatClient(pipeline))
    assert isinstance(agent, Agent)
    response = await agent.run("What is the strangler fig pattern?")
    assert "The answer." in response.text
    assert "https://a.com/doc" in response.text  # sources footer
    assert pipeline.calls[0][0] == "What is the strangler fig pattern?"


async def test_prior_messages_become_history():
    pipeline = FakePipeline()
    agent = build_hosted_agent(PipelineChatClient(pipeline))
    await agent.run(
        [
            Message(role="user", contents=["first question"]),
            Message(role="assistant", contents=["first answer"]),
            Message(role="user", contents=["follow-up"]),
        ]
    )
    query, history = pipeline.calls[0]
    assert query == "follow-up"
    assert {"role": "user", "content": "first question"} in history
    assert {"role": "assistant", "content": "first answer"} in history


async def test_streaming_yields_full_answer():
    pipeline = FakePipeline()
    agent = build_hosted_agent(PipelineChatClient(pipeline))
    chunks = []
    stream = agent.run("q", stream=True)
    async for update in stream:
        chunks.append(update.text if hasattr(update, "text") else str(update))
    assert "The answer." in "".join(chunks)
