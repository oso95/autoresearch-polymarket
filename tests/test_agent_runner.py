import json
import os
import pytest
from src.io_utils import read_jsonl
from src.runner.agent_runner import AgentRunner, build_agent_context, score_prediction

def test_build_agent_context(tmp_path):
    agent_dir = tmp_path / "agent-001"
    agent_dir.mkdir()
    (agent_dir / "strategy.md").write_text("# My Strategy\nBuy when up")
    (agent_dir / "memory.md").write_text("# Strategy Memory\n\n## Current Version\nv1.0\n")
    (agent_dir / "notes.md").write_text("No notes yet")
    (agent_dir / "results.tsv").write_text("")
    scripts_dir = agent_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "calc.py").write_text("print('hello')")
    snapshot = {"chainlink_btc_price": {"price": 65000}, "binance_candles_5m": {"candles": []}}
    ctx = build_agent_context(str(agent_dir), snapshot)
    assert "# My Strategy" in ctx
    assert "v1.0" in ctx
    assert "65000" in ctx
    assert "calc.py" in ctx

def test_score_prediction_correct():
    assert score_prediction("Up", "Up") is True
    assert score_prediction("Down", "Down") is True

def test_score_prediction_wrong():
    assert score_prediction("Up", "Down") is False
    assert score_prediction("Down", "Up") is False

def test_agent_runner_discover_agents(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent-001-test").mkdir()
    (agents_dir / "agent-001-test" / "strategy.md").write_text("")
    (agents_dir / "agent-002-test").mkdir()
    (agents_dir / "agent-002-test" / "strategy.md").write_text("")
    runner = AgentRunner(str(agents_dir), str(tmp_path / "data"))
    agents = runner.discover_agents()
    assert len(agents) == 2
    assert "agent-001-test" in agents
    assert "agent-002-test" in agents

def test_record_prediction_tracks_revisions_and_scores_latest(tmp_path):
    agents_dir = tmp_path / "agents"
    data_dir = tmp_path / "data"
    (data_dir / "rounds" / "1710000000" / "predictions").mkdir(parents=True)
    (data_dir / "rounds" / "1710000000" / "prediction-updates").mkdir(parents=True)
    (data_dir / "live").mkdir(parents=True)
    agent_dir = agents_dir / "agent-001-test"
    agent_dir.mkdir(parents=True)
    (agent_dir / "strategy.md").write_text("test strategy")

    runner = AgentRunner(str(agents_dir), str(data_dir))
    runner.record_prediction("agent-001-test", 1710000000, "Up", 0.6, "first", "v1")
    runner.record_prediction("agent-001-test", 1710000000, "Down", 0.7, "second", "v1")

    preds = read_jsonl(str(agent_dir / "predictions.jsonl"))
    assert len(preds) == 2
    assert preds[0]["revision"] == 1
    assert preds[0]["active"] is False
    assert preds[1]["revision"] == 2
    assert preds[1]["active"] is True

    runner.score_round("agent-001-test", 1710000000, "Down")
    preds = read_jsonl(str(agent_dir / "predictions.jsonl"))
    assert preds[0]["correct"] is None
    assert preds[1]["correct"] is True

    shared_path = data_dir / "rounds" / "1710000000" / "predictions" / "agent-001-test.json"
    shared = json.loads(shared_path.read_text())
    assert shared["revision"] == 2
    assert shared["correct"] is True

    updates_path = data_dir / "rounds" / "1710000000" / "prediction-updates" / "agent-001-test.jsonl"
    updates = read_jsonl(str(updates_path))
    assert len(updates) == 2
