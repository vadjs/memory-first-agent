"""The composition root: settings → wired object graph.

The one place construction knowledge lives. Every surface — CLI, HTTP API,
hosted server — builds through here, never through each other. Imports are
lazy so that importing this module (or any surface) stays light and
credential-free; credentials are only touched at construction time."""

from agent.config import Settings, get_settings


def build_memory(settings: Settings | None = None):
    """Memory tier only — for admin verbs and offline analytics, which must not
    construct web or chat clients (nor touch their credentials)."""
    from agent.embeddings import AzureEmbedder
    from agent.memory import MemoryStore

    settings = settings or get_settings()
    return MemoryStore(settings.redis_url, AzureEmbedder(settings))


def build_util(settings: Settings | None = None):
    from agent.llm import UtilityLLM

    return UtilityLLM(settings or get_settings())


def build_pipeline(settings: Settings | None = None):
    from agent.llm import ConversationLLM
    from agent.pipeline import Pipeline
    from agent.web import ContentFetcher, SearchClient

    settings = settings or get_settings()
    memory = build_memory(settings)
    pipeline = Pipeline(
        settings,
        memory,
        SearchClient(settings),
        ContentFetcher(settings),
        ConversationLLM(settings),
        build_util(settings),
    )
    return settings, memory, pipeline


def build_api_app():
    from agent.api import create_app
    from agent.ratelimit import TokenBucket
    from agent.sessions import SessionStore

    settings, memory, pipeline = build_pipeline()
    return create_app(
        pipeline,
        SessionStore(memory.r),
        settings,
        TokenBucket(memory.r, settings.rate_limit_per_min),
    )
