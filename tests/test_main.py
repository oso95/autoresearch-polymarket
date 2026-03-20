# tests/test_main.py
import json
import os
import pytest
from src.main import (
    init_project,
    _build_live_features,
    _get_current_round_timestamp,
    _round_is_tradeable,
)

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
    assert os.path.exists(os.path.join(project_dir, "data", "shared_knowledge", "forum-guide.md"))

def test_get_current_round_timestamp(tmp_path):
    data_dir = tmp_path / "data" / "live"
    data_dir.mkdir(parents=True)
    (data_dir / "current-round.json").write_text(json.dumps({"round_timestamp": 1710000000}))
    assert _get_current_round_timestamp(str(tmp_path / "data")) == 1710000000

def test_round_is_tradeable():
    assert _round_is_tradeable(1710000000, 15, now=1710000200) is True
    assert _round_is_tradeable(1710000000, 15, now=1710000286) is False

def test_build_live_features():
    snapshot = {
        "binance_orderbook": {
            "bids": [{"price": 100.0, "qty": 2.0}, {"price": 99.5, "qty": 1.0}],
            "asks": [{"price": 100.5, "qty": 3.0}, {"price": 101.0, "qty": 1.0}],
        },
        "binance_trades_recent": {
            "trades": [
                {"p": "100.0", "q": "1.5", "m": False},
                {"p": "100.2", "q": "0.5", "m": True},
                {"p": "100.4", "q": "2.0", "m": False},
            ],
        },
        "polymarket_orderbooks": {
            "books": {
                "Up": {
                    "bids": [{"price": 0.54, "size": 10}],
                    "asks": [{"price": 0.56, "size": 8}],
                },
                "Down": {
                    "bids": [{"price": 0.44, "size": 9}],
                    "asks": [{"price": 0.46, "size": 7}],
                },
            },
        },
    }
    features = _build_live_features(snapshot)
    assert features["binance_mid_price"] == 100.25
    assert features["recent_trade_count_100"] == 3
    assert features["recent_trade_last_price"] == 100.4
    assert features["polymarket_up_best_bid"] == 0.54
    assert features["polymarket_mid_skew"] == pytest.approx(0.10)
