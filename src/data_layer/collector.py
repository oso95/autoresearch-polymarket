# src/data_layer/collector.py
import asyncio
import json
import logging
import os
import time

from src.config import Config
from src.io_utils import atomic_write_json
from src.data_layer.binance_ws import BinanceWebSocket, BinanceStreams
from src.data_layer.polymarket_ws import PolymarketMarketWS, PolymarketRTDS
from src.data_layer.rest_poller import BinanceRestPoller
from src.data_layer.round_manager import RoundManager

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
        timestamp = int(time.time())
        self.round_manager.freeze_snapshot(timestamp)
        logger.info(f"New market detected, round {timestamp} started")

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

        logger.info("Collector starting connections...")

        tasks = [
            asyncio.create_task(binance.connect()),
            asyncio.create_task(poly_market.connect()),
            asyncio.create_task(poly_rtds.connect()),
            asyncio.create_task(poller.run()),
            asyncio.create_task(self._heartbeat_loop()),
        ]

        await asyncio.sleep(2)
        self.write_status(ready=True, stale=False)
        logger.info("Collector ready")

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
