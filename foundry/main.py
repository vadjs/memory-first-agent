"""Foundry Hosted Agent entrypoint (ADR-0009).

Foundry's remote build runs this file at the zip root; the `agent` package sits
alongside it. Configuration arrives exclusively through environment variables set
on the hosted-agent version — no files, no secrets in code."""

from agent_framework_foundry_hosting import ResponsesHostServer

from agent.config import get_settings
from agent.embeddings import AzureEmbedder
from agent.hosted import PipelineChatClient, build_hosted_agent
from agent.llm import ConversationLLM, UtilityLLM
from agent.memory import MemoryStore
from agent.pipeline import Pipeline
from agent.web import ContentFetcher, SearchClient


def create_server() -> ResponsesHostServer:
    settings = get_settings()
    memory = MemoryStore(settings.redis_url, AzureEmbedder(settings))
    pipeline = Pipeline(
        settings,
        memory,
        SearchClient(settings),
        ContentFetcher(settings),
        ConversationLLM(settings),
        UtilityLLM(settings),
    )
    agent = build_hosted_agent(PipelineChatClient(pipeline))
    return ResponsesHostServer(agent)


if __name__ == "__main__":
    create_server().run()
