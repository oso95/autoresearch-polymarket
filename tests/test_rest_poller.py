import pytest
from src.data_layer.rest_poller import BinanceRestPoller, parse_open_interest, parse_long_short_ratio


def test_parse_open_interest():
    raw = [{"symbol": "BTCUSDT", "sumOpenInterest": "10659.509", "sumOpenInterestValue": "365028283.98", "timestamp": 1710000000000}]
    result = parse_open_interest(raw)
    assert result[0]["open_interest"] == 10659.509
    assert result[0]["open_interest_value"] == 365028283.98


def test_parse_long_short_ratio():
    raw = [{"symbol": "BTCUSDT", "longShortRatio": "1.5432", "longAccount": "0.6067", "shortAccount": "0.3933", "timestamp": 1710000000000}]
    result = parse_long_short_ratio(raw)
    assert result[0]["long_short_ratio"] == 1.5432
    assert result[0]["long_pct"] == 0.6067
    assert result[0]["short_pct"] == 0.3933


def test_poller_endpoints():
    poller = BinanceRestPoller(data_dir="/tmp/test")
    assert len(poller.endpoints) == 5
