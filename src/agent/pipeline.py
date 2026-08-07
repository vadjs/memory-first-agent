"""Memory-first turn orchestration (spec §4.2, §5.5).

Plain-async control flow over framework-independent components; the routing
logic *is* the architecture, so it lives here and nowhere else."""

import time
import uuid
from dataclasses import dataclass

from agent.chunker import chunk_markdown
from agent.config import Settings
from agent.guardrails import (
    AT_CAPACITY_MESSAGE,
    REFUSAL_MESSAGE,
    Preflight,
    preflight,
    screen_chunks,
    validate_citations,
)
from agent.memory import ChunkHit, MemoryStore
from agent.prompts import build_synthesis_user
from agent.telemetry import StageTiming, TurnRecord, Usage, cost_usd, log_turn
from agent.web import ContentFetcher, SearchClient, strip_structural

MAX_CONTEXT_CHUNKS = 12
DEGRADED_PREFIX = (
    "⚠ Web search is currently unavailable; this answer relies on possibly stale or "
    "incomplete memory.\n\n"
)
UNAVAILABLE_MESSAGE = (
    "Web search is currently unavailable and I have nothing relevant in memory, "
    "so I can't answer this reliably. Please try again later."
)


@dataclass
class TurnResult:
    answer: str
    route: str
    sources: list[dict]
    record: TurnRecord


class _Stopwatch:
    def __init__(self):
        self.stages: list[StageTiming] = []
        self._t = time.perf_counter()

    def lap(self, stage: str) -> None:
        now = time.perf_counter()
        self.stages.append({"stage": stage, "ms": round((now - self._t) * 1000, 1)})
        self._t = now


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        search: SearchClient,
        fetcher: ContentFetcher,
        conv,
        util,
    ):
        self.settings = settings
        self.memory = memory
        self.search = search
        self.fetcher = fetcher
        self.conv = conv
        self.util = util

    def _is_fresh(self, ts: float, temporal: str) -> bool:
        if temporal == "volatile":
            return False
        if temporal == "slow":
            return ts >= time.time() - self.settings.slow_ttl_days * 86400
        return True  # static

    def _drain_embed_usages(self) -> list[Usage]:
        usages = getattr(self.memory.embedder, "usages", None)
        if usages:
            drained = list(usages)
            usages.clear()
            return drained
        return []

    async def _promote(self, pf: Preflight, answer: str, cited: list[str], route: str) -> None:
        """Promotion invariant (ADR-0001): cache every clean synthesis, except
        volatile / degraded / refused / PII-flagged turns."""
        if route not in ("hit_chunks", "miss_web"):
            return
        if pf.contains_pii or pf.temporal == "volatile":
            return
        await self.memory.put_qa(pf.standalone_query, answer, cited, pf.topic, pf.temporal)

    async def answer_turn(
        self, query: str, history: list[dict] | None = None, session_id: str = ""
    ) -> TurnResult:
        if len(query) > self.settings.max_query_chars:
            raise ValueError(f"query exceeds {self.settings.max_query_chars} characters")
        history = history or []
        watch = _Stopwatch()
        usages: list[Usage] = []
        scores: dict[str, float] = {}

        pf = await preflight(query, history, self.util)
        if pf.usage:
            usages.append(pf.usage)
        watch.lap("preflight")

        def finish(route: str, answer: str, cited: list[str], error: str = "") -> TurnResult:
            usages.extend(self._drain_embed_usages())
            record = TurnRecord(
                turn_id=uuid.uuid4().hex[:12],
                query=query,
                route=route,  # type: ignore[arg-type]
                topic=pf.topic,
                temporal=pf.temporal,
                injection_flagged=pf.is_injection,
                contains_pii=pf.contains_pii,
                scores=scores,
                stages=watch.stages,
                usages=usages,
                total_cost_usd=round(sum(cost_usd(u) for u in usages), 6),
                cited_urls=cited,
                session_id=session_id,
                error=error,
            )
            log_turn(record)
            return TurnResult(answer, route, [{"url": u} for u in cited], record)

        if pf.is_injection:
            return finish("refused", REFUSAL_MESSAGE, [])

        sq = pf.standalone_query
        borderline: list[ChunkHit] = []

        if pf.temporal != "volatile":
            cache = await self.memory.search_cache(sq)
            if cache:
                scores["cache_top"] = round(cache.similarity, 4)
            watch.lap("cache_lookup")
            if (
                cache
                and cache.similarity >= self.settings.cache_threshold
                and self._is_fresh(cache.created_at, pf.temporal)
            ):
                return finish("hit_cache", cache.answer, cache.urls)

            hits = await self.memory.search_chunks(sq, self.settings.top_k)
            if hits:
                scores["chunk_top"] = round(hits[0].similarity, 4)
            watch.lap("chunk_lookup")
            fresh = [h for h in hits if self._is_fresh(h.fetched_at, pf.temporal)]
            if fresh and fresh[0].similarity >= self.settings.chunk_threshold:
                context = [h for h in fresh if h.similarity >= self.settings.borderline_floor]
                try:
                    raw, usage = await self.conv.synthesize(build_synthesis_user(sq, context))
                except Exception:
                    return finish("refused", AT_CAPACITY_MESSAGE, [], error="synthesis_failed")
                usages.append(usage)
                watch.lap("synthesis")
                answer, cited = validate_citations(raw, {h.url for h in context})
                await self._promote(pf, answer, cited, "hit_chunks")
                return finish("hit_chunks", answer, cited)
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
        watch.lap("web_search")
        pages = await self.fetcher.fetch_all([r.url for r in results]) if results else []
        watch.lap("fetch")

        if not pages:
            if self.settings.degraded_answers and borderline:
                try:
                    raw, usage = await self.conv.synthesize(build_synthesis_user(sq, borderline))
                except Exception:
                    return finish("refused", AT_CAPACITY_MESSAGE, [], error="synthesis_failed")
                usages.append(usage)
                answer, cited = validate_citations(raw, {h.url for h in borderline})
                return finish("degraded", DEGRADED_PREFIX + answer, cited, error="web_unavailable")
            return finish("refused", UNAVAILABLE_MESSAGE, [], error="web_unavailable")

        now = time.time()
        context: list[ChunkHit] = []
        to_store: list[dict] = []
        for page in pages:
            page_chunks = chunk_markdown(strip_structural(page.markdown))
            screened, screen_usage = await screen_chunks(page_chunks, self.util)
            if screen_usage:
                usages.append(screen_usage)
            for chunk, quarantined in screened:
                to_store.append(
                    {
                        "text": chunk.text,
                        "url": page.url,
                        "title": page.title,
                        "section": chunk.section,
                        "quarantined": quarantined,
                    }
                )
                if not quarantined and len(context) < MAX_CONTEXT_CHUNKS:
                    context.append(
                        ChunkHit(
                            key="",
                            text=chunk.text,
                            url=page.url,
                            title=page.title,
                            section=chunk.section,
                            fetched_at=now,
                            similarity=0.0,
                        )
                    )
        watch.lap("screen")
        await self.memory.upsert_chunks(to_store)
        for page in pages:
            await self.memory.mark_url_ingested(page.url, self.settings.slow_ttl_days)
        watch.lap("ingest")

        context.extend(borderline)
        if not context:
            return finish("refused", UNAVAILABLE_MESSAGE, [], error="no_usable_content")
        try:
            raw, usage = await self.conv.synthesize(build_synthesis_user(sq, context))
        except Exception:
            return finish("refused", AT_CAPACITY_MESSAGE, [], error="synthesis_failed")
        usages.append(usage)
        watch.lap("synthesis")
        allowed = {p.url for p in pages} | {h.url for h in borderline}
        answer, cited = validate_citations(raw, allowed)
        await self._promote(pf, answer, cited, "miss_web")
        return finish("miss_web", answer, cited)
