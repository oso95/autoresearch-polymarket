import json
import time
import pytest
from src.runner.health_monitor import HealthMonitor

def test_is_data_layer_healthy(tmp_path):
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    hb = {"timestamp": int(time.time() * 1000)}
    (live_dir / "heartbeat.json").write_text(json.dumps(hb))
    monitor = HealthMonitor(str(tmp_path))
    assert monitor.is_data_layer_healthy() is True

def test_is_data_layer_stale(tmp_path):
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    hb = {"timestamp": int((time.time() - 60) * 1000)}
    (live_dir / "heartbeat.json").write_text(json.dumps(hb))
    monitor = HealthMonitor(str(tmp_path), stale_threshold_seconds=30)
    assert monitor.is_data_layer_healthy() is False

def test_is_data_layer_missing(tmp_path):
    monitor = HealthMonitor(str(tmp_path))
    assert monitor.is_data_layer_healthy() is False

def test_is_round_stale(tmp_path):
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    status = {"ready": True, "stale": True, "timestamp": int(time.time() * 1000)}
    (live_dir / "status.json").write_text(json.dumps(status))
    monitor = HealthMonitor(str(tmp_path))
    assert monitor.is_round_data_stale() is True
