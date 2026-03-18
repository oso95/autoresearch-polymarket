import json
import pytest
from src.data_layer.binance_ws import parse_kline_message, parse_depth_message, BinanceStreams

def test_parse_kline_message():
    msg = {
        "e": "kline",
        "s": "BTCUSDT",
        "k": {
            "t": 1710000000000,
            "T": 1710000299999,
            "o": "65000.00",
            "h": "65100.00",
            "l": "64900.00",
            "c": "65050.00",
            "v": "125.432",
            "n": 1543,
            "x": False,
            "q": "8150000.00",
            "V": "62.100",
            "Q": "4035000.00"
        }
    }
    candle = parse_kline_message(msg)
    assert candle["open"] == 65000.00
    assert candle["high"] == 65100.00
    assert candle["low"] == 64900.00
    assert candle["close"] == 65050.00
    assert candle["volume"] == 125.432
    assert candle["trades"] == 1543
    assert candle["open_time"] == 1710000000000
    assert candle["closed"] is False

def test_parse_depth_message():
    msg = {
        "lastUpdateId": 123456,
        "bids": [["65000.00", "1.5"], ["64999.00", "2.0"]],
        "asks": [["65001.00", "1.0"], ["65002.00", "3.0"]]
    }
    book = parse_depth_message(msg)
    assert len(book["bids"]) == 2
    assert book["bids"][0] == {"price": 65000.00, "qty": 1.5}
    assert len(book["asks"]) == 2
    assert book["asks"][0] == {"price": 65001.00, "qty": 1.0}

def test_binance_streams_url():
    streams = BinanceStreams(["btcusdt@kline_5m", "btcusdt@depth20@100ms"])
    url = streams.url
    assert "stream.binance.com" in url
    assert "btcusdt@kline_5m" in url
    assert "btcusdt@depth20@100ms" in url
