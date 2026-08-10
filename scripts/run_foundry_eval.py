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
    async with project_client.get_openai_client(agent_name=AGENT_NAME) as openai_client:
        for question in GOLDEN_QUESTIONS:
            response = await openai_client.responses.create(input=question)
            answer = response.output_text
            hits = await memory.search_chunks(question, k=5)
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


async def main() -> None:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        sys.exit("set FOUNDRY_PROJECT_ENDPOINT (azd env get-values -e mfa-prod)")

    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential) as project_client,
    ):
        items = await gather_items(project_client)
        evals = FoundryEvals(
            project_client=project_client,
            model="gpt-5.6-luna",
            evaluators=[FoundryEvals.GROUNDEDNESS, FoundryEvals.RELEVANCE],
        )
        results = evals.evaluate(items, eval_name="memory-first-agent golden set")
        if inspect.isawaitable(results):
            results = await results
        print(f"status: {results.status}")
        print(f"result counts: {results.result_counts}")
        print(f"per evaluator: {results.per_evaluator}")
        print(f"report: {results.report_url}")


if __name__ == "__main__":
    asyncio.run(main())
