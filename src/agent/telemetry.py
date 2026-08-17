"""Turn telemetry: one structured record per turn, with cost accounting.

The JSONL turn log is the system of record (ADR-0002); Redis memory is not.
Per-turn accounting flows through the TurnMeter: usage arrives as return values
at each seam and is added here — never drained from another module's state.
"""

import json
import os
import time
import uuid
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

_unpriced_warned: set[str] = set()


class StageTiming(TypedDict):
    stage: str
    ms: float


@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int


def cost_usd(usage: Usage) -> float:
    if usage.model not in PRICES:
        # A renamed deployment must not silently report free turns — cost analytics
        # is the system's primary lever (spec §10), so the blind spot is loud.
        if usage.model not in _unpriced_warned:
            _unpriced_warned.add(usage.model)
            log.warning("unpriced_model", model=usage.model)
        return 0.0
    in_price, out_price = PRICES[usage.model]
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


class TurnMeter:
    """Per-turn accounting: stage timings, model usages, similarity scores —
    one accumulator, one place cost math lives, one `finish` into a TurnRecord."""

    def __init__(self):
        self.stages: list[StageTiming] = []
        self.usages: list[Usage] = []
        self.scores: dict[str, float] = {}
        self._t = time.perf_counter()

    def lap(self, stage: str) -> None:
        now = time.perf_counter()
        self.stages.append({"stage": stage, "ms": round((now - self._t) * 1000, 1)})
        self._t = now

    def add(self, usage: Usage | None) -> None:
        if usage is not None:
            self.usages.append(usage)

    def score(self, name: str, value: float) -> None:
        self.scores[name] = round(value, 4)

    def finish(
        self,
        *,
        query: str,
        route: Route,
        topic: str,
        temporal: Temporal,
        injection_flagged: bool,
        contains_pii: bool,
        cited_urls: list[str],
        session_id: str = "",
        error: str = "",
    ) -> TurnRecord:
        return TurnRecord(
            turn_id=uuid.uuid4().hex[:12],
            query=query,
            route=route,
            topic=topic,
            temporal=temporal,
            injection_flagged=injection_flagged,
            contains_pii=contains_pii,
            scores=self.scores,
            stages=self.stages,
            usages=self.usages,
            total_cost_usd=round(sum(cost_usd(u) for u in self.usages), 6),
            cited_urls=cited_urls,
            session_id=session_id,
            error=error,
        )


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
