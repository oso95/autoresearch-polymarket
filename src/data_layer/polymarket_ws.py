# src/data_layer/polymarket_ws.py
import asyncio
import json
import logging

import websockets

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
MARKET_HEARTBEAT_INTERVAL_SECONDS = 5


def _unique_asset_ids(asset_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for asset_id in asset_ids:
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        unique.append(asset_id)
    return unique

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
        "assets_ids": _unique_asset_ids(asset_ids),
        "type": "market",
        "custom_feature_enabled": True,
    }


def build_market_subscription_update(asset_ids: list[str]) -> dict:
    return {
        "operation": "subscribe",
        "assets_ids": _unique_asset_ids(asset_ids),
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
        self._heartbeat_task: asyncio.Task | None = None

    async def _heartbeat_loop(self):
        while self._running and self._ws:
            try:
                await self._ws.send("PING")
            except Exception:
                break
            await asyncio.sleep(MARKET_HEARTBEAT_INTERVAL_SECONDS)

    async def _start_heartbeat(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _stop_heartbeat(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def connect(self, initial_asset_ids: list[str] | None = None):
        self._running = True
        self._subscribed_assets = _unique_asset_ids(initial_asset_ids or [])
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(
                    MARKET_WS_URL,
                    ping_interval=None,
                    ping_timeout=None,
                ) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("Polymarket Market WS connected")
                    if self._subscribed_assets:
                        sub = build_market_subscription(self._subscribed_assets)
                        await ws.send(json.dumps(sub))
                    await self._start_heartbeat()
                    async for raw in ws:
                        try:
                            if raw == "PONG":
                                continue
                            parsed = json.loads(raw)
                            # Handle both single messages and arrays
                            msgs = parsed if isinstance(parsed, list) else [parsed]
                            for msg in msgs:
                                if isinstance(msg, dict):
                                    await self._dispatch(msg)
                        except json.JSONDecodeError:
                            pass
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning(f"Polymarket Market WS disconnected: {e}. Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                await self._stop_heartbeat()
                self._ws = None

    async def subscribe(self, asset_ids: list[str]):
        new_assets = [asset_id for asset_id in _unique_asset_ids(asset_ids) if asset_id not in self._subscribed_assets]
        if not new_assets:
            return
        self._subscribed_assets.extend(new_assets)
        if self._ws:
            sub = build_market_subscription_update(new_assets)
            await self._ws.send(json.dumps(sub))

    async def _dispatch(self, msg: dict):
        if not msg:
            return
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
        await self._stop_heartbeat()
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
