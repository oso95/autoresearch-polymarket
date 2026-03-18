# src/data_layer/polymarket_ws.py
import asyncio
import json
import logging

import websockets

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

def parse_book_event(event: dict) -> dict:
    return {
        "asset_id": event.get("asset_id"),
        "market_id": event.get("market"),
        "bids": [{"price": float(b["price"]), "size": float(b["size"])} for b in event.get("bids", [])],
        "asks": [{"price": float(a["price"]), "size": float(a["size"])} for a in event.get("asks", [])],
        "timestamp": event.get("timestamp"),
    }

def parse_new_market_event(event: dict) -> dict:
    return {
        "market_id": event.get("market"),
        "asset_id": event.get("asset_id"),
        "description": event.get("description"),
    }

def parse_market_resolved_event(event: dict) -> dict:
    return {
        "market_id": event.get("market"),
        "asset_id": event.get("asset_id"),
        "winning_outcome": event.get("winning_outcome"),
    }

def parse_rtds_price(msg: dict) -> dict:
    return {
        "symbol": msg["symbol"],
        "price": float(msg["price"]),
        "timestamp": msg["timestamp"],
    }

def build_market_subscription(asset_ids: list[str]) -> dict:
    return {
        "assets_ids": asset_ids,
        "type": "market",
        "custom_feature_enabled": True,
    }

class PolymarketMarketWS:
    def __init__(self, on_book=None, on_new_market=None, on_resolved=None, on_trade=None):
        self._on_book = on_book
        self._on_new_market = on_new_market
        self._on_resolved = on_resolved
        self._on_trade = on_trade
        self._ws = None
        self._running = False
        self._subscribed_assets: list[str] = []

    async def connect(self, initial_asset_ids: list[str] | None = None):
        self._running = True
        self._subscribed_assets = initial_asset_ids or []
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(MARKET_WS_URL) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("Polymarket Market WS connected")
                    if self._subscribed_assets:
                        sub = build_market_subscription(self._subscribed_assets)
                        await ws.send(json.dumps(sub))
                    hb_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                continue
                            try:
                                parsed = json.loads(raw)
                                # Handle both single messages and arrays
                                msgs = parsed if isinstance(parsed, list) else [parsed]
                                for msg in msgs:
                                    if isinstance(msg, dict):
                                        await self._dispatch(msg)
                            except json.JSONDecodeError:
                                pass
                    finally:
                        hb_task.cancel()
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning(f"Polymarket Market WS disconnected: {e}. Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def subscribe(self, asset_ids: list[str]):
        self._subscribed_assets.extend(asset_ids)
        if self._ws:
            sub = build_market_subscription(asset_ids)
            await self._ws.send(json.dumps(sub))

    async def _heartbeat(self, ws):
        while self._running:
            try:
                await ws.send("PING")
                await asyncio.sleep(10)
            except Exception:
                break

    async def _dispatch(self, msg: dict):
        event_type = msg.get("event_type", "")
        if event_type == "book" and self._on_book:
            await self._on_book(parse_book_event(msg))
        elif event_type == "new_market" and self._on_new_market:
            await self._on_new_market(parse_new_market_event(msg))
        elif event_type == "market_resolved" and self._on_resolved:
            await self._on_resolved(parse_market_resolved_event(msg))
        elif event_type == "last_trade_price" and self._on_trade:
            await self._on_trade(msg)

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()


class PolymarketRTDS:
    def __init__(self, on_price=None):
        self._on_price = on_price
        self._ws = None
        self._running = False

    async def connect(self, topics: list[str] | None = None):
        topics = topics or ["btc/usd"]
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(RTDS_WS_URL) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("Polymarket RTDS connected")
                    sub = {"type": "subscribe", "topics": topics}
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        if raw == "ping":
                            await ws.send("pong")
                            continue
                        msg = json.loads(raw)
                        if "price" in msg and self._on_price:
                            await self._on_price(parse_rtds_price(msg))
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning(f"RTDS disconnected: {e}. Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
