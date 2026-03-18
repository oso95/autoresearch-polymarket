# tests/test_main.py
import os
import pytest
from src.main import init_project

def test_init_project(tmp_path):
    project_dir = str(tmp_path / "project")
    init_project(project_dir)
    assert os.path.isdir(os.path.join(project_dir, "data", "live"))
    assert os.path.isdir(os.path.join(project_dir, "data", "polling"))
    assert os.path.isdir(os.path.join(project_dir, "data", "history"))
    assert os.path.isdir(os.path.join(project_dir, "data", "rounds"))
    assert os.path.isdir(os.path.join(project_dir, "data", "coordinator"))
    assert os.path.isdir(os.path.join(project_dir, "agents"))
    assert os.path.exists(os.path.join(project_dir, "config.json"))
