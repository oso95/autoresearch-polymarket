# tests/test_polymarket_ws.py
import pytest
from src.data_layer.polymarket_ws import (
    parse_book_event,
    parse_new_market_event,
    parse_market_resolved_event,
    parse_rtds_price,
    build_market_subscription,
)

def test_parse_book_event():
    event = {
        "event_type": "book",
        "asset_id": "token123",
        "market": "market456",
        "bids": [{"price": "0.55", "size": "100"}],
        "asks": [{"price": "0.60", "size": "200"}],
        "timestamp": "1710000000"
    }
    book = parse_book_event(event)
    assert book["asset_id"] == "token123"
    assert book["bids"][0] == {"price": 0.55, "size": 100.0}
    assert book["asks"][0] == {"price": 0.60, "size": 200.0}

def test_parse_new_market_event():
    event = {
        "event_type": "new_market",
        "market": "market789",
        "asset_id": "token_up",
        "description": "BTC Up or Down 5m",
    }
    market = parse_new_market_event(event)
    assert market["market_id"] == "market789"
    assert market["asset_id"] == "token_up"

def test_parse_market_resolved_event():
    event = {
        "event_type": "market_resolved",
        "market": "market789",
        "asset_id": "token_up",
        "winning_outcome": "Up",
    }
    result = parse_market_resolved_event(event)
    assert result["market_id"] == "market789"
    assert result["winning_outcome"] == "Up"

def test_parse_rtds_price():
    msg = {
        "symbol": "btc/usd",
        "timestamp": 1710000000000,
        "price": "65432.10"
    }
    price = parse_rtds_price(msg)
    assert price["symbol"] == "btc/usd"
    assert price["price"] == 65432.10
    assert price["timestamp"] == 1710000000000

def test_build_market_subscription():
    sub = build_market_subscription(["token_up", "token_down"])
    assert sub["type"] == "market"
    assert sub["assets_ids"] == ["token_up", "token_down"]
    assert sub["custom_feature_enabled"] is True
