"""Page ingestion (spec §5.5, §7; ADR-0006, ADR-0011) — the acquisition stage of
a miss turn, behind one interface: `acquire(urls, meter, temporal) → IngestResult`.

Fetched pages are chunked, screened chunk-by-chunk (quarantined content is stored
for audit, invisible everywhere else), summarized from clean chunks only, and
stored with provenance; the synthesis context is assembled summary-first under a
fixed budget. URLs ingested recently are not re-fetched: their screened chunks
are reused from the Knowledge Base instead (ADR-0006) — but only when fresh for
the query's temporal class (volatile queries always fetch live), and a marker
with nothing reusable behind it is ignored: the URL is fetched again."""

import asyncio
import time
from dataclasses import dataclass, field, replace

from agent.chunker import chunk_markdown
from agent.config import Settings
from agent.domain import SUMMARY_SECTION, Temporal, is_fresh
from agent.guardrails import SupportsJson, has_injection_markers, screen_chunks
from agent.memory import ChunkHit, ChunkRecord, MemoryStore
from agent.summarizer import summarize_page
from agent.telemetry import TurnMeter
from agent.web import ContentFetcher, strip_structural

MAX_CONTEXT_CHUNKS = 12


def _clean_title(title: str) -> str:
    """Titles get the body-text treatment (spec §8): structural sanitation, then
    the deterministic marker screen — an instruction-like title is dropped, never
    repaired, before it can steer the summarizer or enter the Knowledge Base."""
    title = strip_structural(title)
    return "" if has_injection_markers(title) else title


@dataclass
class IngestResult:
    context: list[ChunkHit] = field(default_factory=list)  # summary-first, budgeted
    fetched_pages: int = 0
    reused_urls: int = 0


class Ingestion:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        fetcher: ContentFetcher,
        util: SupportsJson,
    ):
        self.settings = settings
        self.memory = memory
        self.fetcher = fetcher
        self.util = util

    async def acquire(self, urls: list[str], meter: TurnMeter, temporal: Temporal) -> IngestResult:
        now = time.time()
        fresh_urls: list[str] = []
        reused: list[list[ChunkHit]] = []
        for url in urls:
            if await self.memory.url_recently_ingested(url):
                # Freshness is routing (ADR-0006): reuse may serve only chunks
                # fresh for THIS query's temporal class — volatile is never fresh,
                # so volatile queries always fetch live.
                stored = [
                    h
                    for h in await self.memory.chunks_for_url(url)
                    if is_fresh(
                        h.fetched_at,
                        temporal,
                        now=now,
                        slow_ttl_days=self.settings.slow_ttl_days,
                    )
                ]
                if stored:
                    reused.append(stored)
                    continue
                # A live marker with nothing reusable behind it (stale for this
                # query, chunks evicted by `memory cleanup`, or every chunk
                # quarantined) must not suppress the fetch: honoring it would pin
                # an empty context — misread downstream as "web unavailable" —
                # until the marker expires.
            fresh_urls.append(url)

        pages = await self.fetcher.fetch_all(fresh_urls) if fresh_urls else []
        pages = [replace(p, title=_clean_title(p.title)) for p in pages]
        meter.lap("fetch")

        per_page_chunks = [chunk_markdown(strip_structural(p.markdown)) for p in pages]
        screen_results = await asyncio.gather(
            *(screen_chunks(chunks, self.util) for chunks in per_page_chunks)
        )
        meter.lap("screen")
        # Summarize each page's *clean* chunks (FR-3/FR-5, ADR-0011): quarantined
        # content never reaches the summarizer, so classify-never-rewrite holds.
        summaries = await asyncio.gather(
            *(
                summarize_page(
                    page.title,
                    page.url,
                    [chunk.text for chunk, quarantined in screened if not quarantined],
                    self.util,
                )
                for page, (screened, _) in zip(pages, screen_results, strict=True)
            )
        )
        meter.lap("summarize")

        context: list[ChunkHit] = []
        to_store: list[ChunkRecord] = []
        reusable_pages: list[str] = []
        for page, (screened, screen_usage), (summary, summary_usage) in zip(
            pages, screen_results, summaries, strict=True
        ):
            meter.add(screen_usage)
            meter.add(summary_usage)
            clean: list[tuple[str, str]] = []  # (text, section), summary first
            if summary:
                to_store.append(
                    ChunkRecord(
                        text=summary,
                        url=page.url,
                        title=page.title,
                        section=SUMMARY_SECTION,
                    )
                )
                clean.append((summary, SUMMARY_SECTION))
            # pos preserves document order (summary = 0) so a reused page can be
            # reassembled deterministically, exactly as a fresh fetch reads.
            for pos, (chunk, quarantined) in enumerate(screened, start=1):
                to_store.append(
                    ChunkRecord(
                        text=chunk.text,
                        url=page.url,
                        title=page.title,
                        section=chunk.section,
                        quarantined=quarantined,
                        pos=pos,
                    )
                )
                if not quarantined:
                    clean.append((chunk.text, chunk.section))
            if clean:
                reusable_pages.append(page.url)
            for text, section in clean[: max(0, MAX_CONTEXT_CHUNKS - len(context))]:
                context.append(
                    ChunkHit(
                        key="",
                        text=text,
                        url=page.url,
                        title=page.title,
                        section=section,
                        fetched_at=now,
                        similarity=0.0,
                    )
                )
        if to_store:
            _, usage = await self.memory.upsert_chunks(to_store)
            meter.add(usage)
        # The marker is a promise that chunks_for_url will return content, so only
        # pages that stored a clean chunk or summary earn one; a fully quarantined
        # page stays eligible for re-fetching.
        for url in reusable_pages:
            await self.memory.mark_url_ingested(url, self.settings.slow_ttl_days)

        # Recently ingested pages: reuse their screened chunks instead of paying
        # fetch + screen + summarize again. Summary-first, same budget.
        for stored in reused:
            stored.sort(key=lambda h: h.section != SUMMARY_SECTION)
            context.extend(stored[: max(0, MAX_CONTEXT_CHUNKS - len(context))])
        meter.lap("ingest")

        return IngestResult(context=context, fetched_pages=len(pages), reused_urls=len(reused))
