"""Turn telemetry: one structured record per turn, with cost accounting.

The JSONL turn log is the system of record (ADR-0002); Redis memory is not.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict

import structlog

from agent.domain import Route, Temporal

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("agent")

# USD per 1M tokens (input, output) — Azure list prices, ADR-0005
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5-nano": (0.05, 0.40),
    "text-embedding-3-small": (0.02, 0.0),
}


class StageTiming(TypedDict):
    stage: str
    ms: float


@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int


def cost_usd(usage: Usage) -> float:
    in_price, out_price = PRICES.get(usage.model, (0.0, 0.0))
    return (usage.input_tokens * in_price + usage.output_tokens * out_price) / 1_000_000


@dataclass
class TurnRecord:
    turn_id: str
    query: str
    route: Route
    topic: str
    temporal: Temporal
    injection_flagged: bool
    contains_pii: bool
    scores: dict[str, float]
    stages: list[StageTiming]
    usages: list[Usage]
    total_cost_usd: float
    cited_urls: list[str]
    session_id: str = ""
    error: str = ""
    extras: dict[str, str] = field(default_factory=dict)


def _log_dir() -> Path:
    return Path(os.environ.get("AGENT_LOG_DIR", "logs"))


def log_turn(rec: TurnRecord) -> None:
    payload = asdict(rec)
    log.info("turn", **payload)
    # The file sink is best-effort: sandboxed runtimes (e.g. Foundry Hosted Agents)
    # mount a read-only filesystem, where stdout JSON is the canonical sink instead.
    try:
        directory = _log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "turns.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("turn_log_file_unavailable", error=str(e))


def read_turns() -> list[dict]:
    path = _log_dir() / "turns.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
