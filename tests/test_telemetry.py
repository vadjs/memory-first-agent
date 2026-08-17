import json
import uuid

import pytest

from agent.domain import Route, Temporal
from agent.telemetry import TurnMeter, TurnRecord, Usage, cost_usd, log_turn, read_turns


def test_cost_luna():
    assert cost_usd(Usage("gpt-5.6-luna", 1_000_000, 1_000_000)) == pytest.approx(1.40)


def test_cost_nano():
    assert cost_usd(Usage("gpt-5-nano", 1_000_000, 1_000_000)) == pytest.approx(0.45)


def test_cost_embeddings():
    assert cost_usd(Usage("text-embedding-3-small", 1_000_000, 0)) == pytest.approx(0.02)


def test_cost_unknown_model_is_zero_but_loud(capsys):
    # A renamed deployment must not silently report free turns (spec §10).
    # Unique name per run: warn-once state is process-global, so a fixed name
    # would fail on any rerun within the same interpreter.
    model = f"mystery-{uuid.uuid4().hex[:8]}"
    assert cost_usd(Usage(model, 1000, 1000)) == 0.0
    assert "unpriced_model" in capsys.readouterr().out


def test_unpriced_warning_fires_once_per_model(capsys):
    model = f"mystery-{uuid.uuid4().hex[:8]}"
    cost_usd(Usage(model, 1, 1))
    cost_usd(Usage(model, 1, 1))
    assert capsys.readouterr().out.count("unpriced_model") == 1


def test_turn_meter_accumulates_and_finishes():
    meter = TurnMeter()
    meter.lap("preflight")
    meter.add(Usage("gpt-5-nano", 1_000_000, 0))
    meter.add(None)  # a seam with nothing to report — ignored
    meter.score("cache_top", 0.87654)
    rec = meter.finish(
        query="q",
        route=Route.MISS_WEB,
        topic="technology",
        temporal=Temporal.STATIC,
        injection_flagged=False,
        contains_pii=False,
        cited_urls=["https://a.com"],
        session_id="s-1",
    )
    assert [s["stage"] for s in rec.stages] == ["preflight"]
    assert rec.usages == [Usage("gpt-5-nano", 1_000_000, 0)]
    assert rec.total_cost_usd == pytest.approx(0.05)
    assert rec.scores == {"cache_top": 0.8765}
    assert rec.session_id == "s-1" and rec.turn_id


def test_log_turn_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path))
    rec = TurnRecord(
        turn_id="t-1",
        query="what is redis",
        route=Route.MISS_WEB,
        topic="technology",
        temporal=Temporal.STATIC,
        injection_flagged=False,
        contains_pii=False,
        scores={"cache_top": 0.31, "chunk_top": 0.44},
        stages=[{"stage": "preflight", "ms": 812.0}],
        usages=[Usage("gpt-5-nano", 400, 90)],
        total_cost_usd=cost_usd(Usage("gpt-5-nano", 400, 90)),
        cited_urls=["https://redis.io/docs"],
    )
    log_turn(rec)
    turns = read_turns()
    assert len(turns) == 1
    assert turns[0]["route"] == "miss_web"
    assert turns[0]["cited_urls"] == ["https://redis.io/docs"]
    # file is valid JSONL
    raw = (tmp_path / "turns.jsonl").read_text().strip()
    assert json.loads(raw)["turn_id"] == "t-1"


def test_log_turn_survives_readonly_filesystem(monkeypatch, capsys):
    # A file inside a *file* path can never be created — simulates a read-only sandbox.
    monkeypatch.setenv("AGENT_LOG_DIR", "/dev/null/logs")
    rec = TurnRecord(
        turn_id="t-ro",
        query="q",
        route=Route.HIT_CACHE,
        topic="technology",
        temporal=Temporal.STATIC,
        injection_flagged=False,
        contains_pii=False,
        scores={},
        stages=[],
        usages=[],
        total_cost_usd=0.0,
        cited_urls=[],
    )
    log_turn(rec)  # must not raise; stdout record still emitted
    assert "t-ro" in capsys.readouterr().out
