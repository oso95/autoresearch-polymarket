import os
import pytest
from src.coordinator.crossover import CrossPollinator

def test_copy_script(tmp_path):
    agents_dir = tmp_path / "agents"
    source = agents_dir / "agent-001-test"
    source_scripts = source / "scripts"
    source_scripts.mkdir(parents=True)
    (source_scripts / "calc.py").write_text("def calc(): return 42")
    (source / "strategy.md").write_text("")
    (source / "notes.md").write_text("")
    target = agents_dir / "agent-002-test"
    target_scripts = target / "scripts"
    target_scripts.mkdir(parents=True)
    (target / "strategy.md").write_text("")
    (target / "notes.md").write_text("")

    cp = CrossPollinator(str(agents_dir))
    cp.copy_script("agent-001-test", "agent-002-test", "calc.py", "This script has 68% accuracy for order book analysis")

    assert (target_scripts / "calc.py").exists()
    notes = (target / "notes.md").read_text()
    assert "calc.py" in notes
    assert "68%" in notes

def test_add_suggestion(tmp_path):
    agents_dir = tmp_path / "agents"
    target = agents_dir / "agent-001-test"
    target.mkdir(parents=True)
    (target / "notes.md").write_text("# Notes\n")

    cp = CrossPollinator(str(agents_dir))
    cp.add_suggestion("agent-001-test", "Try using funding rate as a contrarian signal")

    notes = (target / "notes.md").read_text()
    assert "funding rate" in notes
    assert "Coordinator Suggestion" in notes
