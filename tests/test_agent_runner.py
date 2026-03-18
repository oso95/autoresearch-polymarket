import json
import os
import pytest
from src.runner.agent_runner import AgentRunner, build_agent_context, score_prediction

def test_build_agent_context(tmp_path):
    agent_dir = tmp_path / "agent-001"
    agent_dir.mkdir()
    (agent_dir / "strategy.md").write_text("# My Strategy\nBuy when up")
    (agent_dir / "notes.md").write_text("No notes yet")
    (agent_dir / "results.tsv").write_text("")
    scripts_dir = agent_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "calc.py").write_text("print('hello')")
    snapshot = {"chainlink_btc_price": {"price": 65000}, "binance_candles_5m": {"candles": []}}
    ctx = build_agent_context(str(agent_dir), snapshot)
    assert "# My Strategy" in ctx
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
