import asyncio
import json
import logging
from dataclasses import dataclass

import websockets
from websockets.exceptions import InvalidStatus

logger = logging.getLogger(__name__)

BASE_URL = "wss://stream.binance.com:9443/stream?streams="

@dataclass
class BinanceStreams:
    streams: list[str]

    @property
    def url(self) -> str:
        return BASE_URL + "/".join(self.streams)

def parse_kline_message(msg: dict) -> dict:
    k = msg["k"]
    return {
        "open_time": k["t"],
        "close_time": k["T"],
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "quote_volume": float(k["q"]),
        "trades": k["n"],
        "taker_buy_volume": float(k["V"]),
        "taker_buy_quote_volume": float(k["Q"]),
        "closed": k["x"],
    }

def parse_depth_message(msg: dict) -> dict:
    return {
        "last_update_id": msg.get("lastUpdateId"),
        "bids": [{"price": float(p), "qty": float(q)} for p, q in msg["bids"]],
        "asks": [{"price": float(p), "qty": float(q)} for p, q in msg["asks"]],
    }

class BinanceWebSocket:
    def __init__(self, streams: BinanceStreams, on_kline=None, on_depth=None, on_trade=None):
        self.streams = streams
        self._on_kline = on_kline
        self._on_depth = on_depth
        self._on_trade = on_trade
        self._ws = None
        self._running = False
        self._geo_blocked = False

    async def connect(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.streams.url) as ws:
                    self._ws = ws
                    self._geo_blocked = False
                    backoff = 1
                    logger.info("Binance WS connected")
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        event = data.get("e", "")
                        if event == "kline" and self._on_kline:
                            await self._on_kline(parse_kline_message(data))
                        elif "bids" in data and "asks" in data and self._on_depth:
                            await self._on_depth(parse_depth_message(data))
                        elif event == "trade" and self._on_trade:
                            await self._on_trade(data)
            except InvalidStatus as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code == 451:
                    if not self._geo_blocked:
                        logger.warning("Binance WS geo-blocked (HTTP 451); disabling websocket stream and relying on REST/polymarket data")
                        self._geo_blocked = True
                    await asyncio.sleep(300)
                    continue
                if not self._running:
                    break
                logger.warning(f"Binance WS rejected connection: {e}. Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning(f"Binance WS disconnected: {e}. Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
