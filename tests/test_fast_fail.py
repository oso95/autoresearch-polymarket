import json
import os
import pytest
from src.runner.fast_fail import FastFailChecker
from src.io_utils import atomic_append_jsonl

@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agent-001-test"
    d.mkdir()
    (d / "strategy.md").write_text("# Current strategy")
    (d / "strategy.md.prev").write_text("# Previous strategy")
    (d / "scripts").mkdir()
    return str(d)

def test_no_fast_fail_under_threshold(agent_dir):
    checker = FastFailChecker(streak_threshold=3)
    pred_path = os.path.join(agent_dir, "predictions.jsonl")
    for i in range(2):
        atomic_append_jsonl(pred_path, {"round": i, "correct": False, "outcome": "Up", "prediction": "Down"})
    assert checker.should_revert(agent_dir) is False

def test_fast_fail_at_threshold(agent_dir):
    checker = FastFailChecker(streak_threshold=3)
    pred_path = os.path.join(agent_dir, "predictions.jsonl")
    for i in range(3):
        atomic_append_jsonl(pred_path, {"round": i, "correct": False, "outcome": "Up", "prediction": "Down"})
    assert checker.should_revert(agent_dir) is True

def test_revert_restores_previous_strategy(agent_dir):
    checker = FastFailChecker(streak_threshold=3)
    checker.revert_strategy(agent_dir)
    with open(os.path.join(agent_dir, "strategy.md")) as f:
        assert f.read() == "# Previous strategy"

def test_no_revert_if_no_prev(tmp_path):
    d = tmp_path / "agent-no-prev"
    d.mkdir()
    (d / "strategy.md").write_text("# Current")
    checker = FastFailChecker(streak_threshold=3)
    checker.revert_strategy(str(d))
    with open(os.path.join(str(d), "strategy.md")) as f:
        assert f.read() == "# Current"
