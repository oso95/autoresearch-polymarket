import json
import os

from src.codex_cli import DEFAULT_PREDICTION_MODEL
from src.runner.evolver import StrategyEvolver
from src.io_utils import atomic_append_jsonl, atomic_write_json
from src.shared_knowledge import SharedKnowledgeForum


def _setup_agent(tmp_path):
    agents_dir = tmp_path / "agents"
    data_dir = tmp_path / "data"
    agent_dir = agents_dir / "agent-001-test"
    (data_dir / "shared_knowledge").mkdir(parents=True)
    agent_dir.mkdir(parents=True)
    (agent_dir / "scripts").mkdir()
    (agent_dir / "strategy.md").write_text("# Old strategy\n")
    (agent_dir / "memory.md").write_text(
        "# Strategy Memory for agent-001-test\n\n## Current Version\nv1.0\n\n## Change Log\n\n### v1.0\nChange: Initial strategy.\nWhy: Baseline.\nStatus: active\n"
    )
    (agent_dir / "notes.md").write_text("# Notes\n")
    (agent_dir / "results.tsv").write_text(
        "iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n"
    )
    atomic_write_json(
        str(agent_dir / "status.json"),
        {
            "agent_id": "agent-001-test",
            "archetype": "test",
            "total_rounds": 0,
            "total_correct": 0,
            "all_time_win_rate": 0.0,
            "ew_win_rate": 0.0,
            "model": DEFAULT_PREDICTION_MODEL,
            "last_action": "spawn",
            "last_action_round": None,
            "iterations": 0,
            "consecutive_discards": 0,
            "status": "active",
            "memory_version": "v1.0",
        },
    )
    return str(agents_dir), str(data_dir), str(agent_dir)


def test_apply_evolution_creates_pending_experiment(tmp_path):
    agents_dir, data_dir, agent_dir = _setup_agent(tmp_path)
    forum = SharedKnowledgeForum(os.path.join(data_dir, "shared_knowledge"))
    existing_post = forum.create_post("agent-999-other", "Existing note", "This is useful")
    atomic_append_jsonl(
        os.path.join(agent_dir, "predictions.jsonl"),
        {"round": 1, "correct": True, "prediction": "Up", "outcome": "Up"},
    )
    atomic_append_jsonl(
        os.path.join(agent_dir, "predictions.jsonl"),
        {"round": 2, "correct": False, "prediction": "Down", "outcome": "Up"},
    )

    evolver = StrategyEvolver(agents_dir, data_dir, evaluation_window=2)
    ok = evolver.apply_evolution(
        "agent-001-test",
        {
            "change_description": "tighten threshold",
            "change_summary": "Raised threshold",
            "change_why": "Losses were firing too early",
            "strategy_md": "# New strategy\n",
            "new_scripts": {"signal.py": "print('x')\n"},
            "delete_scripts": [],
            "shared_discovery_title": "Threshold update",
            "shared_discovery": "Raise the severe downtrend threshold to 0.8%.",
            "shared_votes": [{"post_id": existing_post["post_id"], "vote": "up", "reason": "aligned with my findings"}],
            "shared_comments": [{"post_id": existing_post["post_id"], "comment": "I validated this too."}],
        },
    )

    assert ok is True
    with open(os.path.join(agent_dir, "status.json")) as f:
        status = json.load(f)
    assert status["last_action"] == "evolve_pending"
    assert status["current_experiment"]["description"] == "tighten threshold"
    assert status["current_experiment"]["baseline_rounds"] == 2
    assert status["memory_version"] == "v1.1"
    assert os.path.exists(os.path.join(agent_dir, "strategy.md.prev"))
    assert os.path.exists(os.path.join(agent_dir, "scripts.prev"))
    with open(os.path.join(agent_dir, "memory.md")) as f:
        memory = f.read()
    assert "### v1.1" in memory
    assert "Status: pending" in memory
    index = forum._read_index()
    titles = [post["title"] for post in index["posts"]]
    assert "Threshold update" in titles
    existing = next(post for post in index["posts"] if post["post_id"] == existing_post["post_id"])
    assert existing["score"] == 1
    comments = forum._comments_for_post(existing_post["post_id"], limit=5)
    assert any("aligned with my findings" in comment["comment"] for comment in comments)
    assert any("validated this too" in comment["comment"] for comment in comments)


def test_finalize_pending_experiment_discards_and_restores_previous_strategy(tmp_path):
    agents_dir, data_dir, agent_dir = _setup_agent(tmp_path)
    (os.path.join(agent_dir, "strategy.md"))
    (tmp_path / "agents" / "agent-001-test" / "strategy.md").write_text("# New strategy\n")
    (tmp_path / "agents" / "agent-001-test" / "strategy.md.prev").write_text("# Old strategy\n")
    (tmp_path / "agents" / "agent-001-test" / "scripts" / "signal.py").write_text("print('new')\n")
    (tmp_path / "agents" / "agent-001-test" / "scripts.prev").mkdir()
    (tmp_path / "agents" / "agent-001-test" / "scripts.prev" / "signal.py").write_text("print('old')\n")
    atomic_write_json(
        os.path.join(agent_dir, "status.json"),
        {
            "agent_id": "agent-001-test",
            "archetype": "test",
            "total_rounds": 2,
            "total_correct": 2,
            "all_time_win_rate": 1.0,
            "ew_win_rate": 1.0,
            "model": DEFAULT_PREDICTION_MODEL,
            "last_action": "evolve_pending",
            "last_action_round": 1,
            "iterations": 1,
            "consecutive_discards": 0,
            "status": "active",
            "memory_version": "v1.1",
            "current_experiment": {
                "iteration": 1,
                "description": "bad experiment",
                "baseline_win_rate": 1.0,
                "baseline_rounds": 2,
                "applied_at_round": 1,
                "strategy_version": "deadbeef",
                "model": DEFAULT_PREDICTION_MODEL,
                "memory_version": "v1.1",
                "previous_memory_version": "v1.0",
            },
        },
    )
    atomic_append_jsonl(
        os.path.join(agent_dir, "predictions.jsonl"),
        {"round": 2, "correct": False, "prediction": "Up", "outcome": "Down"},
    )
    atomic_append_jsonl(
        os.path.join(agent_dir, "predictions.jsonl"),
        {"round": 3, "correct": False, "prediction": "Up", "outcome": "Down"},
    )

    evolver = StrategyEvolver(agents_dir, data_dir, evaluation_window=2)
    result = evolver.finalize_pending_experiment("agent-001-test")

    assert result["status"] == "discard"
    with open(os.path.join(agent_dir, "strategy.md")) as f:
        assert f.read() == "# Old strategy\n"
    with open(os.path.join(agent_dir, "status.json")) as f:
        status = json.load(f)
    assert status["current_experiment"] is None
    assert status["last_action"] == "discard"
    assert status["memory_version"] == "v1.0"
    with open(os.path.join(agent_dir, "memory.md")) as f:
        memory = f.read()
    assert "### v1.1 Outcome" in memory
    assert "Status: discarded" in memory
