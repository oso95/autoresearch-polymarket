import asyncio
import logging
import time

import aiohttp

from src.io_utils import atomic_write_json

logger = logging.getLogger(__name__)

BINANCE_FUTURES = "https://fapi.binance.com"


def parse_open_interest(raw: list[dict]) -> list[dict]:
    return [
        {
            "open_interest": float(r["sumOpenInterest"]),
            "open_interest_value": float(r["sumOpenInterestValue"]),
            "timestamp": r["timestamp"],
        }
        for r in raw
    ]


def parse_long_short_ratio(raw: list[dict]) -> list[dict]:
    return [
        {
            "long_short_ratio": float(r["longShortRatio"]),
            "long_pct": float(r["longAccount"]),
            "short_pct": float(r["shortAccount"]),
            "timestamp": r["timestamp"],
        }
        for r in raw
    ]


def parse_taker_volume(raw: list[dict]) -> list[dict]:
    return [
        {
            "buy_sell_ratio": float(r["buySellRatio"]),
            "buy_vol": float(r["buyVol"]),
            "sell_vol": float(r["sellVol"]),
            "timestamp": r["timestamp"],
        }
        for r in raw
    ]


def parse_funding_rate(raw: list[dict]) -> list[dict]:
    return [
        {
            "funding_rate": float(r["fundingRate"]),
            "mark_price": float(r.get("markPrice", 0)),
            "funding_time": r["fundingTime"],
        }
        for r in raw
    ]


class BinanceRestPoller:
    def __init__(self, data_dir: str, poll_interval: int = 300):
        self.data_dir = data_dir
        self.poll_interval = poll_interval
        self._running = False
        self.endpoints = [
            {
                "url": f"{BINANCE_FUTURES}/futures/data/openInterestHist",
                "params": {"symbol": "BTCUSDT", "period": "5m", "limit": "30"},
                "file": "open_interest.json",
                "parser": parse_open_interest,
            },
            {
                "url": f"{BINANCE_FUTURES}/futures/data/takerlongshortRatio",
                "params": {"symbol": "BTCUSDT", "period": "5m", "limit": "30"},
                "file": "taker_volume.json",
                "parser": parse_taker_volume,
            },
            {
                "url": f"{BINANCE_FUTURES}/futures/data/globalLongShortAccountRatio",
                "params": {"symbol": "BTCUSDT", "period": "5m", "limit": "30"},
                "file": "long_short_ratio.json",
                "parser": parse_long_short_ratio,
            },
            {
                "url": f"{BINANCE_FUTURES}/futures/data/topLongShortPositionRatio",
                "params": {"symbol": "BTCUSDT", "period": "5m", "limit": "30"},
                "file": "top_trader_ratio.json",
                "parser": parse_long_short_ratio,
            },
            {
                "url": f"{BINANCE_FUTURES}/fapi/v1/fundingRate",
                "params": {"symbol": "BTCUSDT", "limit": "1"},
                "file": "funding_rate.json",
                "parser": parse_funding_rate,
            },
        ]

    async def poll_once(self, session: aiohttp.ClientSession) -> dict[str, bool]:
        results = {}
        for ep in self.endpoints:
            try:
                async with session.get(
                    ep["url"],
                    params=ep["params"],
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 451:
                        logger.warning(f"Geo-blocked: {ep['url']}")
                        results[ep["file"]] = False
                        continue
                    resp.raise_for_status()
                    raw = await resp.json()
                    parsed = ep["parser"](raw)
                    path = f"{self.data_dir}/polling/{ep['file']}"
                    atomic_write_json(
                        path,
                        {
                            "data": parsed,
                            "fetched_at": int(time.time() * 1000),
                            "stale": False,
                        },
                    )
                    results[ep["file"]] = True
            except Exception as e:
                logger.warning(f"REST poll failed for {ep['file']}: {e}")
                results[ep["file"]] = False
        return results

    async def run(self):
        self._running = True
        async with aiohttp.ClientSession() as session:
            while self._running:
                await self.poll_once(session)
                await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._running = False
