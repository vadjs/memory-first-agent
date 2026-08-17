"""Memory-first turn orchestration (spec §4.2, §5.5).

Plain-async control flow over framework-independent components; the routing
logic *is* the architecture, so it lives here and nowhere else. Acquisition
(fetch → screen → summarize → store) belongs to the Ingestion module; per-turn
accounting belongs to the TurnMeter. This module only routes."""

import time
from dataclasses import dataclass
from typing import Protocol

from agent.config import Settings
from agent.domain import QueryTooLongError, Route, Temporal, is_fresh
from agent.guardrails import (
    AT_CAPACITY_MESSAGE,
    REFUSAL_MESSAGE,
    Preflight,
    SupportsJson,
    preflight,
    validate_citations,
)
from agent.ingest import Ingestion, IngestResult
from agent.memory import ChunkHit, MemoryStore
from agent.prompts import build_synthesis_user
from agent.telemetry import TurnMeter, TurnRecord, Usage, log_turn
from agent.web import ContentFetcher, SearchClient

DEGRADED_PREFIX = (
    "⚠ Web search is currently unavailable; this answer relies on possibly stale or "
    "incomplete memory.\n\n"
)
UNAVAILABLE_MESSAGE = (
    "Web search is currently unavailable and I have nothing relevant in memory, "
    "so I can't answer this reliably. Please try again later."
)


class SupportsSynthesis(Protocol):
    async def synthesize(self, user_message: str) -> tuple[str, Usage]: ...


@dataclass
class TurnResult:
    answer: str
    route: Route
    sources: list[dict]
    record: TurnRecord


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        search: SearchClient,
        fetcher: ContentFetcher,
        conv: SupportsSynthesis,
        util: SupportsJson,
    ):
        self.settings = settings
        self.memory = memory
        self.search = search
        self.conv = conv
        self.util = util
        self.ingestion = Ingestion(settings, memory, fetcher, util)

    def _fresh(self, ts: float, temporal: Temporal) -> bool:
        return is_fresh(ts, temporal, now=time.time(), slow_ttl_days=self.settings.slow_ttl_days)

    async def _promote(
        self, pf: Preflight, answer: str, cited: list[str], route: Route
    ) -> Usage | None:
        """Promotion invariant (ADR-0001): cache every clean synthesis, except
        volatile / degraded / refused / PII-flagged turns."""
        if route not in (Route.HIT_CHUNKS, Route.MISS_WEB):
            return None
        if pf.contains_pii or pf.temporal == Temporal.VOLATILE:
            return None
        return await self.memory.put_qa(pf.standalone_query, answer, cited, pf.topic, pf.temporal)

    async def _synthesize(
        self, sq: str, context: list[ChunkHit], meter: TurnMeter
    ) -> tuple[str, list[str]] | None:
        """Grounded synthesis plus citation validation; None means at capacity.
        Citations may only reference URLs actually present in the context."""
        try:
            raw, usage = await self.conv.synthesize(build_synthesis_user(sq, context))
        except Exception:
            return None
        meter.add(usage)
        meter.lap("synthesis")
        return validate_citations(raw, {h.url for h in context})

    async def answer_turn(
        self, query: str, history: list[dict] | None = None, session_id: str = ""
    ) -> TurnResult:
        if len(query) > self.settings.max_query_chars:
            raise QueryTooLongError(f"query exceeds {self.settings.max_query_chars} characters")
        history = history or []
        meter = TurnMeter()

        pf = await preflight(query, history, self.util)
        meter.add(pf.usage)
        meter.lap("preflight")

        def finish(route: Route, answer: str, cited: list[str], error: str = "") -> TurnResult:
            record = meter.finish(
                query=query,
                route=route,
                topic=pf.topic,
                temporal=pf.temporal,
                injection_flagged=pf.is_injection,
                contains_pii=pf.contains_pii,
                cited_urls=cited,
                session_id=session_id,
                error=error,
            )
            log_turn(record)
            return TurnResult(answer, route, [{"url": u} for u in cited], record)

        if pf.is_injection:
            return finish(Route.REFUSED, REFUSAL_MESSAGE, [])

        sq = pf.standalone_query
        borderline: list[ChunkHit] = []

        if pf.temporal != Temporal.VOLATILE:
            cache, usage = await self.memory.search_cache(sq)
            meter.add(usage)
            if cache:
                meter.score("cache_top", cache.similarity)
            meter.lap("cache_lookup")
            if (
                cache
                and cache.similarity >= self.settings.cache_threshold
                and self._fresh(cache.created_at, pf.temporal)
            ):
                return finish(Route.HIT_CACHE, cache.answer, cache.urls)

            hits, usage = await self.memory.search_chunks(sq, self.settings.top_k)
            meter.add(usage)
            if hits:
                meter.score("chunk_top", hits[0].similarity)
            meter.lap("chunk_lookup")
            fresh = [h for h in hits if self._fresh(h.fetched_at, pf.temporal)]
            if fresh and fresh[0].similarity >= self.settings.chunk_threshold:
                context = [h for h in fresh if h.similarity >= self.settings.borderline_floor]
                synth = await self._synthesize(sq, context, meter)
                if synth is None:
                    return finish(Route.REFUSED, AT_CAPACITY_MESSAGE, [], error="synthesis_failed")
                answer, cited = synth
                meter.add(await self._promote(pf, answer, cited, Route.HIT_CHUNKS))
                return finish(Route.HIT_CHUNKS, answer, cited)
            borderline = [
                h
                for h in fresh
                if self.settings.borderline_floor <= h.similarity < self.settings.chunk_threshold
            ]

        # -- miss path: acquire from the web ----------------------------------
        try:
            results = await self.search.search(sq)
        except Exception:
            results = []
        meter.lap("web_search")
        acq = (
            await self.ingestion.acquire([r.url for r in results], meter, pf.temporal)
            if results
            else IngestResult()
        )

        if acq.fetched_pages == 0 and not acq.context:
            if self.settings.degraded_answers and borderline:
                synth = await self._synthesize(sq, borderline, meter)
                if synth is None:
                    return finish(Route.REFUSED, AT_CAPACITY_MESSAGE, [], error="synthesis_failed")
                answer, cited = synth
                return finish(
                    Route.DEGRADED, DEGRADED_PREFIX + answer, cited, error="web_unavailable"
                )
            return finish(Route.REFUSED, UNAVAILABLE_MESSAGE, [], error="web_unavailable")

        # A borderline hit and the reuse path can surface the same KB chunk (a
        # borderline URL that is also a search result); duplicates double-weight
        # evidence and spend context budget twice.
        seen = {(h.url, h.text) for h in acq.context}
        context = acq.context + [h for h in borderline if (h.url, h.text) not in seen]
        if not context:
            return finish(Route.REFUSED, UNAVAILABLE_MESSAGE, [], error="no_usable_content")
        synth = await self._synthesize(sq, context, meter)
        if synth is None:
            return finish(Route.REFUSED, AT_CAPACITY_MESSAGE, [], error="synthesis_failed")
        answer, cited = synth
        meter.add(await self._promote(pf, answer, cited, Route.MISS_WEB))
        return finish(Route.MISS_WEB, answer, cited)
