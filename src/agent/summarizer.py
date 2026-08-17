"""Ingest-time Page Summaries (spec FR-3/FR-5, ADR-0011).

The summarizer runs after the ingest screen and sees only clean chunks, so the
classify-never-rewrite core (spec §8) still holds for untrusted text: quarantined
content never reaches the summarizer, and a summary that itself carries injection
markers is dropped, never repaired."""

from pydantic import BaseModel

from agent.guardrails import SupportsJson, has_injection_markers
from agent.prompts import SUMMARY_SYSTEM, build_summary_user
from agent.telemetry import Usage, log

# Enough context for a faithful digest without paying for the whole page.
_MAX_INPUT_CHUNKS = 8


class SummaryOut(BaseModel):
    summary: str


async def summarize_page(
    title: str, url: str, chunk_texts: list[str], llm: SupportsJson
) -> tuple[str | None, Usage | None]:
    """Condense one page's clean chunks into a stored, retrievable digest.

    A missing summary only loses a retrieval target, so errors degrade to
    "no summary" rather than failing the turn."""
    if not chunk_texts:
        return None, None
    try:
        out, usage = await llm.complete_json(
            SUMMARY_SYSTEM,
            build_summary_user(title, url, chunk_texts[:_MAX_INPUT_CHUNKS]),
            SummaryOut,
        )
    except Exception as e:
        log.warning("summarize_failed", url=url, error=repr(e))
        return None, None
    summary = out.summary.strip()
    if not summary or has_injection_markers(summary):
        log.warning("summary_dropped", url=url)
        return None, usage
    return summary, usage
