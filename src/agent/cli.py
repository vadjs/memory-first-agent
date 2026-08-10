"""CLI over the shared Pipeline: chat REPL, one-shot ask, analytics, memory admin.

Admin verbs (memory, erasure) live here on purpose — they are never exposed
over HTTP (spec §12, attack-surface minimization)."""

import asyncio
import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.config import get_settings
from agent.domain import Route

app = typer.Typer(no_args_is_help=True, add_completion=False)
memory_app = typer.Typer(no_args_is_help=True)
app.add_typer(memory_app, name="memory", help="Inspect, clean, and erase agent memory.")

console = Console()

_BADGES = {
    Route.HIT_CACHE: "[bold green]\\[memory ✓ cache][/]",
    Route.HIT_CHUNKS: "[bold green]\\[memory ✓][/]",
    Route.MISS_WEB: "[bold yellow]\\[web ↯][/]",
    Route.DEGRADED: "[bold red]\\[degraded][/]",
    Route.REFUSED: "[bold red]\\[refused][/]",
}


def _build():
    from agent.embeddings import AzureEmbedder
    from agent.llm import ConversationLLM, UtilityLLM
    from agent.memory import MemoryStore
    from agent.pipeline import Pipeline
    from agent.web import ContentFetcher, SearchClient

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
    return settings, memory, pipeline


def _print_result(result, verbose: bool) -> None:
    console.print()
    console.print(_BADGES.get(result.route, result.route), result.answer)
    if result.sources:
        console.print("[dim]Sources:[/]", ", ".join(s["url"] for s in result.sources))
    if verbose:
        rec = result.record
        total_ms = sum(s["ms"] for s in rec.stages)
        stages = " · ".join(f"{s['stage']} {s['ms']:.0f}ms" for s in rec.stages)
        console.print(
            f"[dim]turn={rec.turn_id} cost=${rec.total_cost_usd:.4f} "
            f"total={total_ms:.0f}ms ({stages})[/]"
        )


async def _run_turn(pipeline, memory, query: str, history: list[dict], verbose: bool):
    await memory.ensure_indexes()
    result = await pipeline.answer_turn(query, history)
    _print_result(result, verbose)
    return result


@app.command()
def ask(
    question: str,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show cost and stage timings."),
):
    """Answer one question and exit."""
    settings, memory, pipeline = _build()
    if len(question) > settings.max_query_chars:
        console.print(f"[red]Question exceeds {settings.max_query_chars} characters.[/]")
        raise typer.Exit(1)
    asyncio.run(_run_turn(pipeline, memory, question, [], verbose))


@app.command()
def chat(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show cost and stage timings."),
):
    """Interactive REPL with rolling conversation history."""
    settings, memory, pipeline = _build()
    console.print("[bold]memory-first agent[/] — type 'exit' to quit")
    history: list[dict] = []

    async def loop():
        await memory.ensure_indexes()
        while True:
            try:
                query = console.input("[bold cyan]you>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not query or query.lower() in {"exit", "quit"}:
                break
            if len(query) > settings.max_query_chars:
                console.print(f"[red]Too long (max {settings.max_query_chars} chars).[/]")
                continue
            with console.status("thinking…"):
                result = await pipeline.answer_turn(query, history)
            _print_result(result, verbose)
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": result.answer})
            history[:] = history[-10:]

    asyncio.run(loop())


@app.command()
def analytics(
    cluster: bool = typer.Option(False, "--cluster", help="Cluster cached questions by topic."),
):
    """Aggregate the turn log: hit rate, topics, cost, latency."""
    from agent.analytics import cluster_questions, summarize

    summary = summarize()
    table = Table(title="Turn analytics", show_header=False)
    for key, value in summary.items():
        table.add_row(key, json.dumps(value) if isinstance(value, dict) else str(value))
    console.print(table)

    if cluster:
        from agent.embeddings import AzureEmbedder
        from agent.llm import UtilityLLM
        from agent.memory import MemoryStore

        settings = get_settings()
        memory = MemoryStore(settings.redis_url, AzureEmbedder(settings))
        clusters = asyncio.run(cluster_questions(memory, UtilityLLM(settings)))
        for c in clusters:
            console.print(Panel("\n".join(c["questions"]) or "(empty)", title=c["label"]))


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Run the HTTP API locally (uvicorn)."""
    import uvicorn

    uvicorn.run("agent.api:app", host=host, port=port)


@memory_app.command()
def stats():
    """Counts and Redis memory usage."""
    _, memory, _ = _build()
    console.print(asyncio.run(memory.stats()))


@memory_app.command()
def cleanup(older_than_days: int = typer.Option(30, help="Evict entries older than this.")):
    """Evict stale entries (routine staleness eviction — not erasure)."""
    _, memory, _ = _build()
    console.print(f"deleted: {asyncio.run(memory.cleanup(older_than_days))}")


@memory_app.command()
def clear(yes: bool = typer.Option(False, "--yes", help="Skip confirmation.")):
    """Delete ALL agent memory."""
    if not yes and not typer.confirm("Delete all agent memory?"):
        raise typer.Exit(0)
    _, memory, _ = _build()
    console.print(f"deleted: {asyncio.run(memory.clear())}")


@memory_app.command()
def forget(
    url: str = typer.Option("", help="Erase everything derived from this URL (both tiers)."),
    question: str = typer.Option("", help="Erase the Answer Cache entry for this question."),
):
    """GDPR erasure by provenance (spec §8.6)."""
    if not url and not question:
        console.print("[red]Provide --url or --question.[/]")
        raise typer.Exit(1)
    _, memory, _ = _build()

    async def run():
        deleted = 0
        if url:
            deleted += await memory.forget_url(url)
        if question:
            deleted += await memory.forget_question(question)
        return deleted

    console.print(f"deleted: {asyncio.run(run())}")


def main() -> None:
    app()
