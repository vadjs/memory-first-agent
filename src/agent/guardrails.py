"""Layered prompt-injection guardrails (spec §8) around a classify-never-rewrite core."""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from agent.chunker import Chunk
from agent.prompts import (
    PREFLIGHT_SYSTEM,
    SCREEN_SYSTEM,
    TAXONOMY,
    build_preflight_user,
    build_screen_user,
)
from agent.telemetry import Usage

_URL = re.compile(r"https?://[^\s)\]>\"']+")

# Layer 1a: deterministic screen for canonical injection markers — zero latency,
# zero cost, and not dependent on classifier quality. The LLM screen is layer 1b.
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+instructions"
    r"|disregard\s+(all\s+)?(previous|prior|your)\s+instructions"
    r"|reveal\s+(your\s+)?(hidden\s+|system\s+)?(prompt|instructions)"
    r"|(system|developer)\s+prompt"
    r"|you\s+are\s+now\s+(?!answering|looking)"
    r"|developer\s+mode"
    r"|\bDAN\b\s+mode)",
    re.IGNORECASE,
)


class SupportsJson(Protocol):
    async def complete_json(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> tuple[BaseModel, Usage]: ...


class PreflightOut(BaseModel):
    is_injection: bool
    temporal: Literal["static", "slow", "volatile"]
    topic: str
    contains_pii: bool
    standalone_query: str


class ScreenOut(BaseModel):
    verdicts: list[Literal["content", "instruction_like"]]


@dataclass
class Preflight:
    is_injection: bool
    temporal: str
    topic: str
    contains_pii: bool
    standalone_query: str
    usage: Usage | None = None


def _safe_default(query: str) -> Preflight:
    # Fail open toward the web (volatile), fail closed toward shared-memory writes (PII true).
    return Preflight(
        is_injection=False,
        temporal="volatile",
        topic="other",
        contains_pii=True,
        standalone_query=query,
    )


async def preflight(query: str, history: list[dict], llm: SupportsJson) -> Preflight:
    if _INJECTION_PATTERNS.search(query):
        return Preflight(
            is_injection=True,
            temporal="volatile",
            topic="other",
            contains_pii=False,
            standalone_query=query,
        )
    try:
        out, usage = await llm.complete_json(
            PREFLIGHT_SYSTEM, build_preflight_user(query, history), PreflightOut
        )
        topic = out.topic if out.topic in TAXONOMY else "other"
        return Preflight(
            is_injection=out.is_injection,
            temporal=out.temporal,
            topic=topic,
            contains_pii=out.contains_pii,
            standalone_query=out.standalone_query.strip() or query,
            usage=usage,
        )
    except Exception:
        return _safe_default(query)


async def screen_chunks(
    chunks: list[Chunk], llm: SupportsJson
) -> tuple[list[tuple[Chunk, bool]], Usage | None]:
    """Classify chunks as content vs instruction-like. Never rewrites (the sanitizer
    paradox — spec §8). Missing or failed verdicts quarantine the chunk: fail closed."""
    if not chunks:
        return [], None
    try:
        out, usage = await llm.complete_json(
            SCREEN_SYSTEM, build_screen_user([c.text for c in chunks]), ScreenOut
        )
        verdicts = out.verdicts
    except Exception:
        return [(c, True) for c in chunks], None
    result = []
    for i, chunk in enumerate(chunks):
        quarantined = verdicts[i] == "instruction_like" if i < len(verdicts) else True
        result.append((chunk, quarantined))
    return result, usage


def validate_citations(answer: str, allowed_urls: set[str]) -> tuple[str, list[str]]:
    """Citations must be a subset of actually-retrieved URLs (spec §8 layer 4)."""
    cited: list[str] = []
    clean = answer
    for url in _URL.findall(answer):
        trimmed = url.rstrip(".,;")
        if trimmed in allowed_urls:
            if trimmed not in cited:
                cited.append(trimmed)
        else:
            clean = clean.replace(url, "[unverified source removed]")
    return clean, cited


REFUSAL_MESSAGE = (
    "I can't help with that request — it looks like an attempt to change how I operate. "
    "I'm happy to answer a question instead."
)

AT_CAPACITY_MESSAGE = (
    "I'm at capacity right now and couldn't process this reliably. Please try again shortly."
)
