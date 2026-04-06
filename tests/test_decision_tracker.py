import json
import os
import pytest

from src.runner.decision_tracker import (
    analyze_agent_decisions,
    build_decision_context_for_agent,
    generate_decision_insights,
    save_agent_decision_profile,
)


def _setup_round(data_dir, round_ts, agent_name, updates, outcome):
    """Create a round with prediction updates and result."""
    round_dir = os.path.join(data_dir, "rounds", str(round_ts))
    updates_dir = os.path.join(round_dir, "prediction-updates")
    os.makedirs(updates_dir, exist_ok=True)

    # Write prediction updates
    updates_path = os.path.join(updates_dir, f"{agent_name}.jsonl")
    with open(updates_path, "w") as f:
        for u in updates:
            f.write(json.dumps(u) + "\n")

    # Write result
    result_path = os.path.join(round_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump({"outcome": outcome, "round_timestamp": round_ts}, f)


def test_basic_revision_tracking(tmp_path):
    data_dir = tmp_path / "data"
    agents_dir = tmp_path / "agents"
    agent_name = "agent-001-test"
    os.makedirs(agents_dir / agent_name)

    # Round 1: 2 revisions, both say Up, outcome Up
    _setup_round(str(data_dir), 1000, agent_name, [
        {"prediction": "Up", "confidence": 0.6, "revision": 1, "predicted_at": 1000050},
        {"prediction": "Up", "confidence": 0.7, "revision": 2, "predicted_at": 1000150},
    ], "Up")

    # Round 2: 2 revisions, flip from Up to Down, outcome Down
    _setup_round(str(data_dir), 2000, agent_name, [
        {"prediction": "Up", "confidence": 0.6, "revision": 1, "predicted_at": 2000050},
        {"prediction": "Down", "confidence": 0.8, "revision": 2, "predicted_at": 2000200},
    ], "Down")

    profile = analyze_agent_decisions(str(data_dir), agent_name)

    assert profile["rounds_analyzed"] == 2
    assert profile["revision_stats"][1]["total"] == 2  # rev 1 appeared in 2 rounds
    assert profile["revision_stats"][2]["total"] == 2

    # Rev 1: Up correct (round1), Up wrong (round2) = 50%
    assert profile["revision_stats"][1]["win_rate"] == 0.5
    # Rev 2: Up correct (round1), Down correct (round2) = 100%
    assert profile["revision_stats"][2]["win_rate"] == 1.0

    # First call: Up correct (r1), Up wrong (r2) = 50%
    assert profile["first_call_wr"] == 0.5
    # Last call: Up correct (r1), Down correct (r2) = 100%
    assert profile["last_call_wr"] == 1.0
    assert profile["first_vs_last"] == "last"


def test_flip_detection(tmp_path):
    data_dir = tmp_path / "data"
    agent_name = "agent-002-flipper"

    # Round with a flip: Up -> Down, outcome is Up (flip hurt)
    _setup_round(str(data_dir), 3000, agent_name, [
        {"prediction": "Up", "confidence": 0.6, "revision": 1, "predicted_at": 3000050},
        {"prediction": "Down", "confidence": 0.7, "revision": 2, "predicted_at": 3000200},
    ], "Up")

    # Round without a flip: Down -> Down, outcome is Down
    _setup_round(str(data_dir), 4000, agent_name, [
        {"prediction": "Down", "confidence": 0.6, "revision": 1, "predicted_at": 4000050},
        {"prediction": "Down", "confidence": 0.8, "revision": 2, "predicted_at": 4000200},
    ], "Down")

    profile = analyze_agent_decisions(str(data_dir), agent_name)

    assert profile["flip_stats"]["total_flips"] == 1
    assert profile["flip_stats"]["flips_helped"] == 0  # flip to Down when outcome was Up
    assert profile["flip_stats"]["flips_hurt"] == 1
    # Round with flip: wrong (flipped to Down, outcome Up)
    assert profile["flip_stats"]["flip_round_wr"] == 0.0
    # Round without flip: correct (stayed Down, outcome Down)
    assert profile["flip_stats"]["no_flip_round_wr"] == 1.0


def test_timing_buckets(tmp_path):
    data_dir = tmp_path / "data"
    agent_name = "agent-003-timer"

    # Early prediction (30s into round), outcome Up
    _setup_round(str(data_dir), 5000, agent_name, [
        {"prediction": "Up", "confidence": 0.6, "revision": 1, "predicted_at": 5000030000},
    ], "Up")

    # Late prediction (250s into round), outcome Down
    _setup_round(str(data_dir), 6000, agent_name, [
        {"prediction": "Down", "confidence": 0.7, "revision": 1, "predicted_at": 6000250000},
    ], "Down")

    profile = analyze_agent_decisions(str(data_dir), agent_name)
    timing = profile["timing_stats"]
    assert "early_0-60s" in timing or "late_180-300s" in timing


def test_save_and_load_profile(tmp_path):
    data_dir = tmp_path / "data"
    agents_dir = tmp_path / "agents"
    agent_name = "agent-004-saver"
    os.makedirs(agents_dir / agent_name)

    _setup_round(str(data_dir), 7000, agent_name, [
        {"prediction": "Up", "confidence": 0.6, "revision": 1, "predicted_at": 7000050},
        {"prediction": "Up", "confidence": 0.7, "revision": 2, "predicted_at": 7000150},
    ], "Up")

    profile = save_agent_decision_profile(str(agents_dir), str(data_dir), agent_name)
    assert profile["rounds_analyzed"] == 1

    # Check file was saved
    saved_path = os.path.join(str(agents_dir), agent_name, "decision_quality.json")
    assert os.path.exists(saved_path)

    with open(saved_path) as f:
        saved = json.load(f)
    assert saved["rounds_analyzed"] == 1


def test_decision_context_for_agent(tmp_path):
    data_dir = tmp_path / "data"
    agents_dir = tmp_path / "agents"
    agent_name = "agent-005-context"
    os.makedirs(agents_dir / agent_name)

    # Create enough rounds for context to be generated (need >= 5)
    for i in range(6):
        _setup_round(str(data_dir), 8000 + i * 1000, agent_name, [
            {"prediction": "Up", "confidence": 0.6, "revision": 1, "predicted_at": (8000 + i * 1000) * 1000 + 50000},
        ], "Up" if i % 2 == 0 else "Down")

    save_agent_decision_profile(str(agents_dir), str(data_dir), agent_name)
    context = build_decision_context_for_agent(str(agents_dir), agent_name)
    assert "Decision Quality Profile" in context
    assert "Revision" in context


def test_generate_insights(tmp_path):
    profiles = [
        {
            "agent": "agent-a",
            "rounds_analyzed": 20,
            "first_vs_last": "first",
            "flip_stats": {"flipping_is_beneficial": False, "flip_round_wr": 0.4, "no_flip_round_wr": 0.6},
            "optimal_revision": 1,
            "timing_stats": {
                "early_0-60s": {"win_rate": 0.55, "wins": 11, "total": 20},
                "late_180-300s": {"win_rate": 0.45, "wins": 9, "total": 20},
            },
        },
        {
            "agent": "agent-b",
            "rounds_analyzed": 20,
            "first_vs_last": "last",
            "flip_stats": {"flipping_is_beneficial": True, "flip_round_wr": 0.65, "no_flip_round_wr": 0.5},
            "optimal_revision": 2,
            "timing_stats": {
                "early_0-60s": {"win_rate": 0.45, "wins": 9, "total": 20},
                "mid_60-180s": {"win_rate": 0.6, "wins": 12, "total": 20},
            },
        },
    ]

    insights = generate_decision_insights(profiles)
    assert "First vs Last Call" in insights
    assert "Flipping Analysis" in insights
    assert "Optimal Revision" in insights
    assert "Timing Analysis" in insights


def test_no_data_returns_empty(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(data_dir)
    profile = analyze_agent_decisions(str(data_dir), "nonexistent-agent")
    assert profile["rounds_analyzed"] == 0


def test_multi_flip_round(tmp_path):
    data_dir = tmp_path / "data"
    agent_name = "agent-006-multiflip"

    # Round with multiple flips: Up -> Down -> Up, outcome Up
    _setup_round(str(data_dir), 9000, agent_name, [
        {"prediction": "Up", "confidence": 0.5, "revision": 1, "predicted_at": 9000050},
        {"prediction": "Down", "confidence": 0.6, "revision": 2, "predicted_at": 9000100},
        {"prediction": "Up", "confidence": 0.7, "revision": 3, "predicted_at": 9000200},
    ], "Up")

    profile = analyze_agent_decisions(str(data_dir), agent_name)

    # Two flip events: Up->Down and Down->Up
    assert profile["flip_stats"]["total_flips"] == 2
    # First flip (Up->Down): flip was wrong (outcome Up), staying was correct
    # Second flip (Down->Up): flip was correct (outcome Up), staying was wrong
    assert profile["flip_stats"]["flips_helped"] == 1
    assert profile["flip_stats"]["flips_hurt"] == 1
