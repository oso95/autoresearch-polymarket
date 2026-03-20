# tests/test_collector.py
import json
import os
import pytest
from src.data_layer.collector import (
    _btc_5m_slug_from_window_start,
    _candidate_btc_5m_slugs,
    Collector,
    _parse_market_window,
    select_btc_5m_market,
    select_btc_5m_market_from_markets,
)

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

def test_select_btc_5m_market():
    events = [
        {
            "slug": "eth-updown-5m-1710000300",
            "title": "Ethereum Up or Down - ...",
            "markets": [{"slug": "eth-updown-5m-1710000300", "clobTokenIds": "[\"a\",\"b\"]", "outcomes": "[\"Up\",\"Down\"]"}],
        },
        {
            "slug": "btc-updown-5m-1710000600",
            "title": "Bitcoin Up or Down - ...",
            "markets": [{
                "id": "m1",
                "slug": "btc-updown-5m-1710000600",
                "conditionId": "cond1",
                "clobTokenIds": "[\"up-token\",\"down-token\"]",
                "outcomes": "[\"Up\",\"Down\"]",
                "outcomePrices": "[\"0.51\",\"0.49\"]",
                "acceptingOrders": True,
                "enableOrderBook": True,
            }],
        },
    ]
    market = select_btc_5m_market(events, now_ts=1710000610)
    assert market["slug"] == "btc-updown-5m-1710000600"
    assert market["token_map"]["Up"] == "up-token"
    assert market["token_map"]["Down"] == "down-token"

def test_parse_market_window_uses_current_et_day():
    start_ts, end_ts = _parse_market_window(
        "Bitcoin Up or Down - March 20, 7:55AM-8:00AM ET",
        now_ts=1773921570,  # 2026-03-19 07:59:30 ET
    )
    assert start_ts == 1774007700
    assert end_ts == 1774008000

def test_btc_5m_slug_from_window_start():
    assert _btc_5m_slug_from_window_start(1773921300) == "btc-updown-5m-1773921300"


def test_candidate_btc_5m_slugs_prefers_current_slot_first():
    slugs = _candidate_btc_5m_slugs(1773931650)  # 2026-03-19 10:47:30 ET
    assert slugs[0] == "btc-updown-5m-1773931500"
    assert "btc-updown-5m-1773931200" in slugs

def test_market_is_tradeable(data_dir):
    collector = Collector(data_dir)
    market = {"round_start_ts": 1710000000}
    assert collector._market_is_tradeable(market, now_ts=1710000000 + 200) is True
    assert collector._market_is_tradeable(market, now_ts=1710000000 + 286) is False


def test_market_is_tradeable_accepting_orders_overrides_window(data_dir):
    collector = Collector(data_dir)
    market = {
        "round_start_ts": 1710000000,
        "window_start_ts": 1710000000,
        "window_end_ts": 1710000300,
        "accepting_orders": True,
    }
    assert collector._market_is_tradeable(market, now_ts=1710000600) is True

def test_select_btc_5m_market_from_markets():
    markets = [
        {
            "id": "old",
            "slug": "btc-updown-5m-1710000600",
            "question": "Bitcoin Up or Down - March 9, 11:00AM-11:05AM ET",
            "startDate": "2024-03-09T16:00:00Z",
            "conditionId": "cond-old",
            "clobTokenIds": "[\"up-old\",\"down-old\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "outcomePrices": "[\"0.52\",\"0.48\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
        },
        {
            "id": "fresh",
            "slug": "btc-updown-5m-1710000900",
            "question": "Bitcoin Up or Down - March 9, 11:14AM-11:19AM ET",
            "startDate": "2024-03-09T16:14:30Z",
            "conditionId": "cond-fresh",
            "clobTokenIds": "[\"up-fresh\",\"down-fresh\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "outcomePrices": "[\"0.55\",\"0.45\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
        },
    ]
    market = select_btc_5m_market_from_markets(markets, now_ts=1710000860)
    assert market["market_id"] == "fresh"
    assert market["token_map"]["Up"] == "up-fresh"


def test_select_btc_5m_market_from_markets_prefers_active_window():
    markets = [
        {
            "id": "closed",
            "slug": "btc-updown-5m-1774007100",
            "question": "Bitcoin Up or Down - March 20, 7:50AM-7:55AM ET",
            "conditionId": "cond-closed",
            "clobTokenIds": "[\"up-closed\",\"down-closed\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "outcomePrices": "[\"0.55\",\"0.45\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
        },
        {
            "id": "active",
            "slug": "btc-updown-5m-1774007400",
            "question": "Bitcoin Up or Down - March 20, 7:55AM-8:00AM ET",
            "conditionId": "cond-active",
            "clobTokenIds": "[\"up-active\",\"down-active\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "outcomePrices": "[\"0.34\",\"0.66\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
        },
    ]
    market = select_btc_5m_market_from_markets(markets, now_ts=1773921570)  # 7:59:30 ET
    assert market["market_id"] == "active"
    assert market["window_start_ts"] == 1774007700
    assert market["window_end_ts"] == 1774008000


def test_select_btc_5m_market_from_markets_falls_back_to_freshest_accepting():
    markets = [
        {
            "id": "older",
            "slug": "btc-updown-5m-1774018800",
            "question": "Bitcoin Up or Down - March 20, 11:00AM-11:05AM ET",
            "conditionId": "cond-older",
            "clobTokenIds": "[\"up-older\",\"down-older\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
            "createdAt": "2026-03-19T15:07:01Z",
            "updatedAt": "2026-03-19T15:17:47Z",
        },
        {
            "id": "fresh",
            "slug": "btc-updown-5m-1774019400",
            "question": "Bitcoin Up or Down - March 20, 11:10AM-11:15AM ET",
            "conditionId": "cond-fresh",
            "clobTokenIds": "[\"up-fresh\",\"down-fresh\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
            "createdAt": "2026-03-19T15:17:03Z",
            "updatedAt": "2026-03-19T15:18:17Z",
        },
    ]
    market = select_btc_5m_market_from_markets(markets, now_ts=1773933537)  # 11:18:57 ET
    assert market["market_id"] == "fresh"


def test_select_btc_5m_market_from_markets_does_not_pick_tomorrow_market():
    markets = [
        {
            "id": "tomorrow",
            "slug": "btc-updown-5m-1774050000",
            "question": "Bitcoin Up or Down - March 20, 7:40PM-7:45PM ET",
            "conditionId": "cond-tomorrow",
            "clobTokenIds": "[\"up-tomorrow\",\"down-tomorrow\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
            "createdAt": "2026-03-19T19:47:27Z",
            "updatedAt": "2026-03-19T19:50:07Z",
        },
        {
            "id": "current",
            "slug": "btc-updown-5m-1773964200",
            "question": "Bitcoin Up or Down - March 19, 7:50PM-7:55PM ET",
            "conditionId": "cond-current",
            "clobTokenIds": "[\"up-current\",\"down-current\"]",
            "outcomes": "[\"Up\",\"Down\"]",
            "acceptingOrders": True,
            "enableOrderBook": True,
            "createdAt": "2026-03-19T19:47:03Z",
            "updatedAt": "2026-03-19T19:50:56Z",
        },
    ]
    market = select_btc_5m_market_from_markets(markets, now_ts=1773964263)  # 7:51:03 PM ET
    assert market["market_id"] == "current"
