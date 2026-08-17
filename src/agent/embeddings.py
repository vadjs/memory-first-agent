"""Embedding clients: Azure OpenAI-backed and a deterministic fake for tests/evals.

`embed` returns its token usage in-band — accounting is a return value at this
seam, never an attribute another module drains."""

import hashlib
import math
from typing import Protocol

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from agent.aoai import client_for
from agent.config import Settings
from agent.telemetry import Usage


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage | None]: ...


class AzureEmbedder:
    dim = 1536

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = client_for(settings)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=4),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage | None]:
        r = await self._client.embeddings.create(
            model=self._settings.embed_deployment,
            input=texts,
            timeout=10.0,
        )
        usage = Usage(self._settings.embed_deployment, r.usage.prompt_tokens, 0)
        return [d.embedding for d in r.data], usage


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

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage | None]:
        out = []
        for t in texts:
            if t in self._vectors:
                v = self._vectors[t]
                norm = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / norm for x in v])
            else:
                out.append(self._hash_vector(t))
        return out, None
