"""Submit a Foundry evaluation run for the deployed hosted agent (portal: Evaluation tab).

Asks the deployed agent the golden questions, pairs each answer with the memory
context it was grounded on, and submits Groundedness + Relevance evaluators as a
cloud eval run on the Foundry project. Prints the portal report URL.

REDIS_URL must point at the SAME memory the deployed agent uses (the cloud
Managed Redis) — judging answers against a different memory's context produces
false groundedness failures. Measured: 3/6 vs 6/6 on identical answers.
"""

import asyncio
import inspect
import os
import sys

from agent_framework import Message
from agent_framework._evaluation import EvalItem  # not re-exported publicly yet
from agent_framework.foundry import FoundryEvals
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from agent.config import Settings
from agent.embeddings import AzureEmbedder
from agent.memory import MemoryStore

GOLDEN_QUESTIONS = [
    "What is the strangler fig pattern in software architecture?",
    "What does the CAP theorem say about distributed systems?",
    "What is Redis and what is it commonly used for?",
    "What is retrieval-augmented generation (RAG)?",
    "What is infrastructure as code?",
    "What is prompt injection in AI security?",
]

AGENT_NAME = "memory-first-agent"


async def gather_items(project_client: AIProjectClient) -> list[EvalItem]:
    settings = Settings()
    memory = MemoryStore(settings.redis_url, AzureEmbedder(settings))
    items = []
    # get_openai_client is added by azure.ai.projects' `_patch` module at runtime;
    # type checkers resolve the generated `_client` class, which lacks it.
    async with project_client.get_openai_client(  # ty: ignore[unresolved-attribute]
        agent_name=AGENT_NAME
    ) as openai_client:
        for question in GOLDEN_QUESTIONS:
            response = await openai_client.responses.create(input=question)
            answer = response.output_text
            hits, _ = await memory.search_chunks(question, k=5)
            context = "\n\n".join(h.text for h in hits) or None
            items.append(
                EvalItem(
                    conversation=[
                        Message(role="user", contents=[question]),
                        Message(role="assistant", contents=[answer]),
                    ],
                    context=context,
                )
            )
            print(f"  collected: {question[:60]}… ({len(answer)} chars)")
    await memory.aclose()
    return items


async def run_dataset_eval(project_client: AIProjectClient) -> None:
    """Project-level dataset eval: golden questions vs memory context (Evaluations list)."""
    items = await gather_items(project_client)
    evals = FoundryEvals(
        project_client=project_client,
        model="gpt-5.6-luna",
        evaluators=[FoundryEvals.GROUNDEDNESS, FoundryEvals.RELEVANCE],
    )
    results = evals.evaluate(items, eval_name="memory-first-agent golden set")
    if inspect.isawaitable(results):
        results = await results
    _report(results)


async def run_trace_eval(project_client: AIProjectClient, lookback_hours: int) -> None:
    """Agent-linked eval over the agent's own OTel traces — this is the kind the
    agent-scoped Evaluation tab lists (data source references the agent id).

    The filter must match `gen_ai.agent.id` as it appears in the traces: for a
    Foundry-hosted agent that is the platform's internal agent GUID (stable
    across versions), not the public agent name. Find it once with:
      dependencies | where name startswith 'invoke_agent'
                   | distinct tostring(customDimensions['gen_ai.agent.id'])
    and pass it via FOUNDRY_AGENT_TRACE_ID."""
    from agent_framework.foundry import evaluate_traces

    trace_agent_id = os.environ.get("FOUNDRY_AGENT_TRACE_ID", AGENT_NAME)
    results = await evaluate_traces(
        project_client=project_client,
        model="gpt-5.6-luna",
        agent_id=trace_agent_id,
        lookback_hours=lookback_hours,
        eval_name="memory-first-agent live traffic",
        timeout=600.0,
    )
    _report(results)


def _report(results) -> None:
    print(f"status: {results.status}")
    print(f"result counts: {results.result_counts}")
    print(f"per evaluator: {results.per_evaluator}")
    print(f"report: {results.report_url}")


async def main() -> None:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        sys.exit("set FOUNDRY_PROJECT_ENDPOINT (azd env get-values -e mfa-prod)")

    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential) as project_client,
    ):
        if "--traces" in sys.argv:
            await run_trace_eval(project_client, lookback_hours=4)
        else:
            await run_dataset_eval(project_client)


if __name__ == "__main__":
    asyncio.run(main())
