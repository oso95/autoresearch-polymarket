# tests/test_collector.py
import json
import os
import pytest
from src.data_layer.collector import Collector

@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)

def test_collector_init_creates_dirs(data_dir):
    collector = Collector(data_dir)
    collector.init_dirs()
    assert os.path.isdir(os.path.join(data_dir, "live"))
    assert os.path.isdir(os.path.join(data_dir, "polling"))
    assert os.path.isdir(os.path.join(data_dir, "history"))
    assert os.path.isdir(os.path.join(data_dir, "rounds"))
    assert os.path.isdir(os.path.join(data_dir, "coordinator"))
    assert os.path.isdir(os.path.join(data_dir, "archive"))

def test_collector_writes_status(data_dir):
    collector = Collector(data_dir)
    collector.init_dirs()
    collector.write_status(ready=True, stale=False)
    status_path = os.path.join(data_dir, "live", "status.json")
    status = json.loads(open(status_path).read())
    assert status["ready"] is True
    assert status["stale"] is False
    assert "timestamp" in status

def test_collector_writes_heartbeat(data_dir):
    collector = Collector(data_dir)
    collector.init_dirs()
    collector.write_heartbeat()
    hb_path = os.path.join(data_dir, "live", "heartbeat.json")
    assert os.path.exists(hb_path)
    hb = json.loads(open(hb_path).read())
    assert "timestamp" in hb
