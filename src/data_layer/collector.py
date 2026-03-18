# src/data_layer/collector.py
import asyncio
import json
import logging
import os
import time

import aiohttp

from src.config import Config
from src.io_utils import atomic_write_json
from src.data_layer.binance_ws import BinanceWebSocket, BinanceStreams
from src.data_layer.polymarket_ws import PolymarketMarketWS, PolymarketRTDS
from src.data_layer.rest_poller import BinanceRestPoller
from src.data_layer.round_manager import RoundManager

GAMMA_API = "https://gamma-api.polymarket.com"

logger = logging.getLogger(__name__)

class Collector:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.round_manager = RoundManager(data_dir)
        self._candles: list[dict] = []
        self._max_candles = 100
        self._trades: list[dict] = []
        self._max_trades = 500
        self._running = False
        self._last_chainlink_price: dict | None = None
        self._last_round_time: float = 0
        self._min_round_interval = 240  # At least 4 min between rounds (5m markets)

    def init_dirs(self):
        for subdir in ["live", "polling", "history", "rounds", "coordinator", "archive"]:
            os.makedirs(os.path.join(self.data_dir, subdir), exist_ok=True)

    def write_status(self, ready: bool, stale: bool):
        atomic_write_json(
            os.path.join(self.data_dir, "live", "status.json"),
            {"ready": ready, "stale": stale, "timestamp": int(time.time() * 1000)},
        )

    def write_heartbeat(self):
        atomic_write_json(
            os.path.join(self.data_dir, "live", "heartbeat.json"),
            {"timestamp": int(time.time() * 1000)},
        )

    async def _on_kline(self, candle: dict):
        if candle["closed"]:
            self._candles.append(candle)
            self._candles = self._candles[-self._max_candles:]
            atomic_write_json(
                os.path.join(self.data_dir, "live", "binance_candles_5m.json"),
                {"candles": self._candles, "updated_at": int(time.time() * 1000)},
            )

    async def _on_depth(self, book: dict):
        atomic_write_json(
            os.path.join(self.data_dir, "live", "binance_orderbook.json"),
            {**book, "updated_at": int(time.time() * 1000)},
        )

    async def _on_trade(self, trade: dict):
        self._trades.append(trade)
        self._trades = self._trades[-self._max_trades:]
        atomic_write_json(
            os.path.join(self.data_dir, "live", "binance_trades_recent.json"),
            {"trades": self._trades, "updated_at": int(time.time() * 1000)},
        )

    async def _on_poly_book(self, book: dict):
        atomic_write_json(
            os.path.join(self.data_dir, "live", "polymarket_orderbook.json"),
            {**book, "updated_at": int(time.time() * 1000)},
        )

    async def _on_new_market(self, market: dict):
        atomic_write_json(
            os.path.join(self.data_dir, "live", "polymarket_market.json"),
            {**market, "updated_at": int(time.time() * 1000)},
        )
        logger.debug(f"New market event: {market.get('market_id', '?')}")

    async def _on_resolved(self, result: dict):
        current = self.round_manager.current_round
        if current:
            open_price = 0.0
            close_price = 0.0
            if self._last_chainlink_price:
                close_price = self._last_chainlink_price.get("price", 0.0)
            self.round_manager.record_resolution(
                current,
                result["winning_outcome"],
                open_price,
                close_price,
            )

    async def _on_chainlink_price(self, price: dict):
        self._last_chainlink_price = price
        atomic_write_json(
            os.path.join(self.data_dir, "live", "chainlink_btc_price.json"),
            {**price, "updated_at": int(time.time() * 1000)},
        )

    async def _backfill_candles(self):
        """Fetch last 100 5-minute candles from Binance REST on startup."""
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://api.binance.com/api/v3/klines"
                params = {"symbol": "BTCUSDT", "interval": "5m", "limit": "100"}
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 451:
                        logger.warning("Binance candle backfill geo-blocked")
                        return
                    raw = await resp.json()
                    candles = []
                    for k in raw:
                        candles.append({
                            "open_time": k[0], "close_time": k[6],
                            "open": float(k[1]), "high": float(k[2]),
                            "low": float(k[3]), "close": float(k[4]),
                            "volume": float(k[5]), "quote_volume": float(k[7]),
                            "trades": k[8], "taker_buy_volume": float(k[9]),
                            "taker_buy_quote_volume": float(k[10]),
                            "closed": True,
                        })
                    self._candles = candles
                    atomic_write_json(
                        os.path.join(self.data_dir, "live", "binance_candles_5m.json"),
                        {"candles": candles, "updated_at": int(time.time() * 1000)},
                    )
                    logger.info(f"Backfilled {len(candles)} candles from Binance REST")
        except Exception as e:
            logger.warning(f"Candle backfill failed: {e}")

    async def _discover_market_tokens(self) -> list[str]:
        """Discover current BTC 5m market token IDs via Gamma API."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{GAMMA_API}/events"
                params = {
                    "series_ticker": "btc-up-or-down-5m",
                    "active": "true",
                    "limit": "3",
                    "order": "startDate",
                    "ascending": "false",
                }
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    events = await resp.json()
                    token_ids = []
                    for event in events:
                        for market in event.get("markets", []):
                            clob_ids = market.get("clobTokenIds")
                            if clob_ids:
                                token_ids.extend(clob_ids)
                            # Store market info
                            atomic_write_json(
                                os.path.join(self.data_dir, "live", "polymarket_market.json"),
                                {
                                    "market_id": market.get("id", ""),
                                    "condition_id": market.get("conditionId", ""),
                                    "slug": event.get("slug", ""),
                                    "outcomes": market.get("outcomes", []),
                                    "outcome_prices": market.get("outcomePrices", []),
                                    "token_ids": clob_ids or [],
                                    "updated_at": int(time.time() * 1000),
                                },
                            )
                    logger.info(f"Discovered {len(token_ids)} market token IDs from {len(events)} events")
                    return token_ids
        except Exception as e:
            logger.warning(f"Market discovery failed: {e}")
            return []

    async def _market_discovery_loop(self, poly_market: PolymarketMarketWS):
        """Periodically discover new markets and subscribe to them."""
        known_tokens: set[str] = set()
        while self._running:
            try:
                token_ids = await self._discover_market_tokens()
                new_tokens = [t for t in token_ids if t not in known_tokens]
                if new_tokens:
                    await poly_market.subscribe(new_tokens)
                    known_tokens.update(new_tokens)
                    logger.info(f"Subscribed to {len(new_tokens)} new market tokens")
            except Exception as e:
                logger.warning(f"Market discovery loop error: {e}")
            await asyncio.sleep(120)  # Re-discover every 2 minutes

    async def _round_timer_loop(self):
        """Create a new round every 5 minutes on a fixed schedule."""
        # Wait for initial warm-up
        await asyncio.sleep(10)
        while self._running:
            timestamp = int(time.time())
            self.round_manager.freeze_snapshot(timestamp)
            self._last_round_time = time.time()
            logger.info(f"=== ROUND {timestamp} === (5-minute timer)")
            await asyncio.sleep(300)  # Exactly 5 minutes

    async def _heartbeat_loop(self):
        while self._running:
            self.write_heartbeat()
            await asyncio.sleep(5)

    async def run(self, config: Config | None = None):
        self.init_dirs()
        self._running = True
        self.write_status(ready=False, stale=False)

        binance = BinanceWebSocket(
            BinanceStreams(["btcusdt@kline_5m", "btcusdt@depth20@100ms", "btcusdt@trade"]),
            on_kline=self._on_kline,
            on_depth=self._on_depth,
            on_trade=self._on_trade,
        )
        poly_market = PolymarketMarketWS(
            on_book=self._on_poly_book,
            on_new_market=self._on_new_market,
            on_resolved=self._on_resolved,
        )
        poly_rtds = PolymarketRTDS(on_price=self._on_chainlink_price)
        poller = BinanceRestPoller(self.data_dir)

        # Backfill historical data before starting streams
        await self._backfill_candles()

        # Discover current BTC 5m market token IDs via Gamma API
        initial_asset_ids = await self._discover_market_tokens()

        logger.info("Collector starting connections...")

        tasks = [
            asyncio.create_task(binance.connect()),
            asyncio.create_task(poly_market.connect(initial_asset_ids=initial_asset_ids)),
            asyncio.create_task(poly_rtds.connect()),
            asyncio.create_task(poller.run()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._market_discovery_loop(poly_market)),
            asyncio.create_task(self._round_timer_loop()),
        ]

        # Wait for initial data to arrive before signaling ready
        await asyncio.sleep(5)
        self.write_status(ready=True, stale=False)
        logger.info("Collector ready — data streams active")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Collector shutting down")
        finally:
            self._running = False
            await binance.close()
            await poly_market.close()
            await poly_rtds.close()
            poller.stop()
            self.write_status(ready=False, stale=True)

    def stop(self):
        self._running = False
