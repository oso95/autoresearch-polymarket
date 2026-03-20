# tests/test_round_manager.py
import json
import os
import pytest
from src.data_layer.round_manager import RoundManager

@pytest.fixture
def data_dir(tmp_path):
    for d in ["live", "polling", "history", "rounds"]:
        (tmp_path / d).mkdir()
    (tmp_path / "live" / "status.json").write_text(json.dumps({"ready": True, "stale": False}))
    (tmp_path / "live" / "chainlink_btc_price.json").write_text(json.dumps({"price": 65000.0, "timestamp": 1710000000000}))
    (tmp_path / "live" / "binance_candles_5m.json").write_text(json.dumps({"candles": []}))
    (tmp_path / "live" / "binance_orderbook.json").write_text(json.dumps({"bids": [], "asks": []}))
    (tmp_path / "live" / "polymarket_orderbook.json").write_text(json.dumps({"bids": [], "asks": []}))
    (tmp_path / "live" / "polymarket_market.json").write_text(json.dumps({}))
    (tmp_path / "polling" / "open_interest.json").write_text(json.dumps({"data": []}))
    (tmp_path / "polling" / "taker_volume.json").write_text(json.dumps({"data": []}))
    (tmp_path / "polling" / "long_short_ratio.json").write_text(json.dumps({"data": []}))
    (tmp_path / "polling" / "top_trader_ratio.json").write_text(json.dumps({"data": []}))
    (tmp_path / "polling" / "funding_rate.json").write_text(json.dumps({"data": []}))
    return str(tmp_path)

def test_freeze_snapshot(data_dir):
    rm = RoundManager(data_dir)
    timestamp = 1710000000
    rm.freeze_snapshot(timestamp)
    snapshot_path = os.path.join(data_dir, "rounds", str(timestamp), "snapshot.json")
    current_round_path = os.path.join(data_dir, "live", "current-round.json")
    predictions_dir = os.path.join(data_dir, "rounds", str(timestamp), "predictions")
    prediction_updates_dir = os.path.join(data_dir, "rounds", str(timestamp), "prediction-updates")
    assert os.path.exists(snapshot_path)
    assert os.path.exists(current_round_path)
    assert os.path.isdir(predictions_dir)
    assert os.path.isdir(prediction_updates_dir)
    snapshot = json.loads(open(snapshot_path).read())
    assert "chainlink_btc_price" in snapshot
    assert "binance_candles_5m" in snapshot
    assert "polymarket_orderbook" in snapshot
    assert "polling" in snapshot

def test_record_resolution(data_dir):
    rm = RoundManager(data_dir)
    timestamp = 1710000000
    os.makedirs(os.path.join(data_dir, "rounds", str(timestamp)))
    rm.record_resolution(timestamp, "Up", 65000.0, 65050.0)
    result_path = os.path.join(data_dir, "rounds", str(timestamp), "result.json")
    assert os.path.exists(result_path)
    result = json.loads(open(result_path).read())
    assert result["outcome"] == "Up"
    assert result["open_price"] == 65000.0
    assert result["close_price"] == 65050.0

def test_record_resolution_appends_to_history(data_dir):
    rm = RoundManager(data_dir)
    timestamp = 1710000000
    os.makedirs(os.path.join(data_dir, "rounds", str(timestamp)))
    rm.record_resolution(timestamp, "Up", 65000.0, 65050.0)
    history_path = os.path.join(data_dir, "history", "resolutions.jsonl")
    assert os.path.exists(history_path)
    with open(history_path) as f:
        line = json.loads(f.readline())
    assert line["outcome"] == "Up"
    assert line["round_timestamp"] == 1710000000
