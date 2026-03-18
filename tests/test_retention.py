# tests/test_retention.py
import json
import os
import time
import pytest
from src.data_layer.retention import RetentionManager

@pytest.fixture
def data_dir(tmp_path):
    for d in ["rounds", "history", "archive"]:
        (tmp_path / d).mkdir()
    return str(tmp_path)

def test_archive_old_rounds(data_dir):
    rm = RetentionManager(data_dir)
    old_ts = int(time.time()) - 90000
    old_round = os.path.join(data_dir, "rounds", str(old_ts))
    os.makedirs(old_round)
    with open(os.path.join(old_round, "snapshot.json"), "w") as f:
        json.dump({"test": True}, f)
    new_ts = int(time.time()) - 60
    new_round = os.path.join(data_dir, "rounds", str(new_ts))
    os.makedirs(new_round)
    with open(os.path.join(new_round, "snapshot.json"), "w") as f:
        json.dump({"test": True}, f)
    rm.archive_old_rounds(max_age_hours=24)
    assert not os.path.exists(old_round)
    assert os.path.exists(new_round)
    archives = os.listdir(os.path.join(data_dir, "archive"))
    assert any(str(old_ts) in a for a in archives)

def test_rotate_predictions(data_dir):
    rm = RetentionManager(data_dir)
    pred_path = os.path.join(data_dir, "history", "predictions.jsonl")
    with open(pred_path, "w") as f:
        for i in range(5):
            f.write(json.dumps({"round": i, "agent": "test"}) + "\n")
    rm.rotate_predictions()
    archives = os.listdir(os.path.join(data_dir, "archive"))
    assert any("predictions-" in a for a in archives)
