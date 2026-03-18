# tests/test_spawner.py
import os
import pytest
from src.coordinator.spawner import AgentSpawner, SEED_STRATEGIES

def test_seed_strategies_exist():
    assert len(SEED_STRATEGIES) == 10
    for seed in SEED_STRATEGIES:
        assert "name" in seed
        assert "strategy" in seed
        assert len(seed["strategy"]) > 50

def test_spawn_from_seed(tmp_path):
    spawner = AgentSpawner(str(tmp_path))
    agent_name = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    agent_dir = os.path.join(str(tmp_path), agent_name)
    assert os.path.isdir(agent_dir)
    assert os.path.exists(os.path.join(agent_dir, "strategy.md"))
    assert os.path.exists(os.path.join(agent_dir, "notes.md"))
    assert os.path.isdir(os.path.join(agent_dir, "scripts"))
    with open(os.path.join(agent_dir, "strategy.md")) as f:
        assert len(f.read()) > 50

def test_clone_agent(tmp_path):
    spawner = AgentSpawner(str(tmp_path))
    source_name = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    source_dir = os.path.join(str(tmp_path), source_name)
    os.makedirs(os.path.join(source_dir, "scripts"), exist_ok=True)
    with open(os.path.join(source_dir, "scripts", "test.py"), "w") as f:
        f.write("print('test')")
    clone_name = spawner.clone_agent(source_name, mutation_note="Try reversing the signal")
    clone_dir = os.path.join(str(tmp_path), clone_name)
    assert os.path.isdir(clone_dir)
    assert os.path.exists(os.path.join(clone_dir, "scripts", "test.py"))
    with open(os.path.join(clone_dir, "notes.md")) as f:
        assert "reversing the signal" in f.read()

def test_spawn_increments_id(tmp_path):
    spawner = AgentSpawner(str(tmp_path))
    name1 = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    name2 = spawner.spawn_from_seed(SEED_STRATEGIES[1])
    assert name1 != name2
    id1 = int(name1.split("-")[1])
    id2 = int(name2.split("-")[1])
    assert id2 == id1 + 1
