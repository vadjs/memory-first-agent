"""Azure OpenAI client construction.

The embedding and utility-LLM adapters speak the OpenAI SDK and share
`client_for` — one key-vs-managed-identity branch. The conversation model's
agent-framework client needs a sync credential, so it branches beside its own
constructor, but consumes `base_url_for`: the endpoint URL scheme is defined
once, here."""

from openai import AsyncOpenAI

from agent.config import Settings


def base_url_for(settings: Settings) -> str:
    """The Azure OpenAI v1-surface base URL every client in the system targets."""
    return f"{settings.azure_openai_endpoint.rstrip('/')}/openai/v1/"


def client_for(settings: Settings) -> AsyncOpenAI:
    base_url = base_url_for(settings)
    if settings.use_managed_identity:
        # AsyncOpenAI awaits a callable api_key — the provider must be the aio variant.
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

        provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        return AsyncOpenAI(base_url=base_url, api_key=provider)
    return AsyncOpenAI(base_url=base_url, api_key=settings.azure_openai_api_key)
