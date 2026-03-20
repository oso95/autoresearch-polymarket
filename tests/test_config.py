# tests/test_config.py
import pytest
from src.config import load_config, Config

def test_load_config_from_file(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"min_agents": 5, "max_agents": 15}')
    config = load_config(str(config_file))
    assert config.min_agents == 5
    assert config.max_agents == 15

def test_load_config_defaults():
    config = Config()
    assert config.min_agents == 3
    assert config.max_agents == 10
    assert config.evaluation_window_rounds == 5
    assert config.fast_fail_streak == 3
    assert config.coordinator_frequency_rounds == 20
    assert config.kill_threshold_win_rate == 0.45
    assert config.kill_min_rounds == 50
    assert config.prediction_deadline_seconds == 90
    assert config.intraround_update_interval_seconds == 5
    assert config.prediction_lock_seconds == 5
    assert config.initial_screening_rounds == 15
