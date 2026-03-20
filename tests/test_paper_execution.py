import asyncio
import pytest

from src.runner import paper_execution
from src.runner.paper_execution import build_execution_quote, build_live_execution_quote, score_execution


def test_build_execution_quote_uses_polymarket_books():
    snapshot = {
        "polymarket_market": {
            "market_id": "m1",
            "slug": "btc-updown-5m-1710000000",
            "token_map": {"Up": "tok-up", "Down": "tok-down"},
        },
        "polymarket_orderbooks": {
            "books": {
                "Up": {
                    "bids": [{"price": 0.52, "size": 100}],
                    "asks": [{"price": 0.54, "size": 100}],
                    "last_trade_price": 0.53,
                },
                "Down": {
                    "bids": [{"price": 0.46, "size": 100}],
                    "asks": [{"price": 0.48, "size": 100}],
                    "last_trade_price": 0.47,
                },
            }
        },
    }
    quote = build_execution_quote(snapshot, "Down")
    assert quote["entry_price"] == 0.48
    assert quote["best_ask"] == 0.48
    assert quote["asset_id"] == "tok-down"
    assert quote["market_slug"] == "btc-updown-5m-1710000000"
    assert quote["order_size_shares"] == 100.0
    assert quote["levels_crossed"] == 1


def test_build_execution_quote_sorts_and_walks_asks():
    snapshot = {
        "polymarket_market": {
            "market_id": "m1",
            "slug": "btc-updown-5m-1710000000",
            "token_map": {"Up": "tok-up", "Down": "tok-down"},
        },
        "polymarket_orderbooks": {
            "books": {
                "Up": {
                    "bids": [{"price": 0.48, "size": 100}],
                    "asks": [
                        {"price": 0.99, "size": 20},
                        {"price": 0.51, "size": 30},
                        {"price": 0.52, "size": 50},
                    ],
                    "last_trade_price": 0.5,
                },
            }
        },
    }
    quote = build_execution_quote(snapshot, "Up")
    assert quote["best_ask"] == 0.51
    assert quote["entry_price"] == pytest.approx((30 * 0.51 + 50 * 0.52 + 20 * 0.99) / 100)
    assert quote["levels_crossed"] == 3


def test_score_execution():
    result = score_execution({"entry_price": 0.4, "prediction": "Up"}, "Up")
    assert result["payout"] == 1.0
    assert result["pnl_per_share"] == 0.6
    assert result["return_pct"] == pytest.approx(1.5)


def test_build_live_execution_quote_prefers_price_endpoint(monkeypatch):
    snapshot = {
        "polymarket_market": {
            "market_id": "m1",
            "slug": "btc-updown-5m-1710000000",
            "token_map": {"Up": "tok-up", "Down": "tok-down"},
        },
        "polymarket_orderbooks": {
            "books": {
                "Down": {
                    "bids": [{"price": 0.49, "size": 100}],
                    "asks": [{"price": 0.5, "size": 100}],
                    "last_trade_price": 0.5,
                },
            }
        },
    }

    async def fake_fetch(_session, token_id, side):
        assert token_id == "tok-down"
        return 0.49 if side == "BUY" else 0.5

    monkeypatch.setattr(paper_execution, "_fetch_market_price", fake_fetch)
    quote = asyncio.run(build_live_execution_quote(snapshot, "Down"))
    assert quote["entry_price"] == 0.5
    assert quote["entry_price_source"] == "polymarket_price_sell"
    assert quote["price_endpoint_bid"] == 0.49
    assert quote["price_endpoint_ask"] == 0.5
    assert quote["best_bid"] == 0.49
    assert quote["best_ask"] == 0.5
