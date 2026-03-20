# tests/test_tournament.py
import json
import os
import pytest
from src.coordinator.tournament import Tournament
from src.coordinator.spawner import AgentSpawner, SEED_STRATEGIES
from src.runner.agent_runner import AgentRunner
from src.config import Config
from src.io_utils import atomic_append_jsonl

@pytest.fixture
def setup(tmp_path):
    agents_dir = str(tmp_path / "agents")
    data_dir = str(tmp_path / "data")
    os.makedirs(os.path.join(data_dir, "coordinator"), exist_ok=True)
    config = Config(min_agents=2, max_agents=5, kill_threshold_win_rate=0.45, kill_min_rounds=10, initial_screening_rounds=5)
    spawner = AgentSpawner(agents_dir)
    runner = AgentRunner(agents_dir, data_dir)
    tournament = Tournament(config, spawner, runner, data_dir)
    return tournament, spawner, runner, agents_dir, data_dir

def test_should_kill_low_performer(setup):
    tournament, spawner, runner, agents_dir, data_dir = setup
    name = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
    for i in range(15):
        atomic_append_jsonl(pred_path, {"round": i, "agent": name, "prediction": "Up", "outcome": "Down", "correct": False, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"})
    assert tournament.should_kill(name) is True

def test_should_not_kill_above_threshold(setup):
    tournament, spawner, runner, agents_dir, data_dir = setup
    name = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
    for i in range(15):
        correct = i % 2 == 0
        atomic_append_jsonl(pred_path, {"round": i, "agent": name, "prediction": "Up", "outcome": "Up" if correct else "Down", "correct": correct, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"})
    assert tournament.should_kill(name) is False

def test_should_not_kill_below_min_rounds(setup):
    tournament, spawner, runner, agents_dir, data_dir = setup
    name = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
    for i in range(5):
        atomic_append_jsonl(pred_path, {"round": i, "agent": name, "prediction": "Up", "outcome": "Down", "correct": False, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"})
    assert tournament.should_kill(name) is False

def test_initial_screening_kill(setup):
    tournament, spawner, runner, agents_dir, data_dir = setup
    name = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
    for i in range(6):
        correct = i == 0
        atomic_append_jsonl(pred_path, {"round": i, "agent": name, "prediction": "Up", "outcome": "Up" if correct else "Down", "correct": correct, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"})
    assert tournament.should_kill(name) is True

def test_tournament_skips_model_variants_under_single_model_policy(setup):
    tournament, spawner, runner, agents_dir, data_dir = setup
    top = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    other = spawner.spawn_from_seed(SEED_STRATEGIES[1])
    for i in range(12):
        atomic_append_jsonl(
            os.path.join(agents_dir, top, "predictions.jsonl"),
            {"round": i, "agent": top, "prediction": "Up", "outcome": "Up", "correct": True, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"}
        )
        atomic_append_jsonl(
            os.path.join(agents_dir, other, "predictions.jsonl"),
            {"round": i, "agent": other, "prediction": "Up", "outcome": "Down", "correct": i % 2 == 0, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"}
        )
    runner.refresh_shared_ledger()
    result = tournament.run_cycle()
    variants = [a for a in result["actions"] if a["type"] == "model-variant"]
    assert variants == []
