"""Foundry Hosted Agent entrypoint (ADR-0009).

Foundry's remote build runs this file at the zip root; the `agent` package sits
alongside it. Configuration arrives exclusively through environment variables set
on the hosted-agent version — no files, no secrets in code."""

import os

from agent_framework.observability import enable_instrumentation
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.core import configure_observability

from agent.config import get_settings
from agent.embeddings import AzureEmbedder
from agent.hosted import PipelineChatClient, build_hosted_agent
from agent.llm import ConversationLLM, UtilityLLM
from agent.memory import MemoryStore
from agent.pipeline import Pipeline
from agent.web import ContentFetcher, SearchClient


def create_server() -> ResponsesHostServer:
    # OTel → Azure Monitor: gen_ai spans for every model call plus server traces,
    # surfaced in the Foundry portal's Traces/Monitor views via the project's
    # App Insights connection. No-op when no connection string is configured.
    # Statsbeat (the exporter's vendor telemetry) probes the IMDS endpoint, which
    # the hosted sandbox blocks — every probe would land as a failed dependency.
    os.environ.setdefault("APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL", "true")
    # Message content in spans is OTel "sensitive data", off by default. Trace-based
    # evaluations need it (they judge the recorded conversations), so this reference
    # environment captures it; set CAPTURE_TRACE_CONTENT=false where data-governance
    # policy forbids conversation content in telemetry — dataset evals still work.
    capture_content = os.environ.get("CAPTURE_TRACE_CONTENT", "true").lower() == "true"
    configure_observability(
        connection_string=os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"),
        enable_sensitive_data=capture_content,
    )
    enable_instrumentation(enable_sensitive_data=capture_content)
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
