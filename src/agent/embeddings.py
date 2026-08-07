"""Embedding clients: Azure OpenAI-backed and a deterministic fake for tests/evals."""

import hashlib
import math
from typing import Protocol

from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from agent.config import Settings
from agent.telemetry import Usage


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _client_for(settings: Settings) -> AsyncOpenAI:
    base_url = f"{settings.azure_openai_endpoint.rstrip('/')}/openai/v1/"
    if settings.use_managed_identity:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        return AsyncOpenAI(base_url=base_url, api_key=provider)
    return AsyncOpenAI(base_url=base_url, api_key=settings.azure_openai_api_key)


class AzureEmbedder:
    dim = 1536

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = _client_for(settings)
        self.usages: list[Usage] = []  # drained by the pipeline per turn

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def embed(self, texts: list[str]) -> list[list[float]]:
        r = await self._client.embeddings.create(
            model=self._settings.embed_deployment,
            input=texts,
            timeout=10.0,
        )
        self.usages.append(Usage(self._settings.embed_deployment, r.usage.prompt_tokens, 0))
        return [d.embedding for d in r.data]


class FakeEmbedder:
    """Deterministic, offline. Identical text → identical unit vector.

    Tests may pin exact vectors per text via `vectors` to control similarity.
    """

    def __init__(self, dim: int = 8, vectors: dict[str, list[float]] | None = None):
        self.dim = dim
        self._vectors = vectors or {}

    def _hash_vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [(digest[i % len(digest)] - 127.5) / 127.5 for i in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            if t in self._vectors:
                v = self._vectors[t]
                norm = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / norm for x in v])
            else:
                out.append(self._hash_vector(t))
        return out
