import json

import pytest

from agent.telemetry import TurnRecord, Usage, cost_usd, log_turn, read_turns


def test_cost_luna():
    assert cost_usd(Usage("gpt-5.6-luna", 1_000_000, 1_000_000)) == pytest.approx(1.40)


def test_cost_nano():
    assert cost_usd(Usage("gpt-5-nano", 1_000_000, 1_000_000)) == pytest.approx(0.45)


def test_cost_embeddings():
    assert cost_usd(Usage("text-embedding-3-small", 1_000_000, 0)) == pytest.approx(0.02)


def test_cost_unknown_model_is_zero():
    assert cost_usd(Usage("mystery-model", 1000, 1000)) == 0.0


def test_log_turn_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path))
    rec = TurnRecord(
        turn_id="t-1",
        query="what is redis",
        route="miss_web",
        topic="technology",
        temporal="static",
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
