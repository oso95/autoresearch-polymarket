# src/data_layer/collector.py
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src.config import Config
from src.io_utils import atomic_write_json
from src.data_layer.binance_ws import BinanceWebSocket, BinanceStreams
from src.data_layer.polymarket_ws import PolymarketMarketWS, PolymarketRTDS
from src.data_layer.rest_poller import BinanceRestPoller
from src.data_layer.round_manager import RoundManager

GAMMA_API = "https://gamma-api.polymarket.com"
NY_TZ = ZoneInfo("America/New_York")
TITLE_WINDOW_RE = re.compile(r"(\d{1,2}:\d{2}(?:AM|PM))-(\d{1,2}:\d{2}(?:AM|PM))\s*ET", re.IGNORECASE)
TITLE_DATE_WINDOW_RE = re.compile(
    r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{1,2}:\d{2}(?:AM|PM))-(\d{1,2}:\d{2}(?:AM|PM))\s*ET",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def _parse_json_array(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return []


def _market_timestamp(candidate: dict) -> int:
    slug = candidate.get("slug") or candidate.get("ticker") or ""
    tail = slug.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _parse_iso_ts(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _parse_market_window(title: str | None, now_ts: int | None = None) -> tuple[int, int]:
    if not title:
        return 0, 0

    now = datetime.fromtimestamp(now_ts or int(time.time()), tz=NY_TZ)
    date_match = TITLE_DATE_WINDOW_RE.search(title)
    if date_match:
        month_name, day_str, start_str, end_str = date_match.groups()
        candidates: list[tuple[float, datetime, datetime]] = []
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                start_dt = datetime.strptime(
                    f"{month_name} {int(day_str)} {year} {start_str.upper()}",
                    "%B %d %Y %I:%M%p",
                ).replace(tzinfo=NY_TZ)
            except ValueError:
                continue
            end_dt = datetime.strptime(
                f"{month_name} {int(day_str)} {year} {end_str.upper()}",
                "%B %d %Y %I:%M%p",
            ).replace(tzinfo=NY_TZ)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            midpoint = start_dt + (end_dt - start_dt) / 2
            distance = abs((midpoint - now).total_seconds())
            candidates.append((distance, start_dt, end_dt))
        if candidates:
            _, start_dt, end_dt = min(candidates, key=lambda item: item[0])
            return int(start_dt.timestamp()), int(end_dt.timestamp())

    match = TITLE_WINDOW_RE.search(title)
    if not match:
        return 0, 0
    start_clock = datetime.strptime(match.group(1).upper(), "%I:%M%p").time()
    end_clock = datetime.strptime(match.group(2).upper(), "%I:%M%p").time()

    candidates: list[tuple[float, datetime, datetime]] = []
    for day_offset in (-1, 0, 1):
        base_day = (now + timedelta(days=day_offset)).date()
        start_dt = datetime.combine(base_day, start_clock, tzinfo=NY_TZ)
        end_dt = datetime.combine(base_day, end_clock, tzinfo=NY_TZ)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        midpoint = start_dt + (end_dt - start_dt) / 2
        distance = abs((midpoint - now).total_seconds())
        candidates.append((distance, start_dt, end_dt))

    _, start_dt, end_dt = min(candidates, key=lambda item: item[0])
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _btc_5m_slug_from_window_start(window_start_ts: int) -> str:
    return f"btc-updown-5m-{window_start_ts}"


def _candidate_btc_5m_slugs(now_ts: int | None = None) -> list[str]:
    now = datetime.fromtimestamp(now_ts or int(time.time()), tz=NY_TZ)
    current_slot = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5)
    candidates = []
    seen = set()
    for minute_offset in (0, 5, -5, 10, -10):
        slot_dt = current_slot + timedelta(minutes=minute_offset)
        slug = _btc_5m_slug_from_window_start(int(slot_dt.timestamp()))
        if slug in seen:
            continue
        seen.add(slug)
        candidates.append(slug)
    return candidates


def _normalize_market_record(market: dict, now_ts: int | None = None) -> dict | None:
    slug = (market.get("slug") or "").lower()
    if not slug.startswith("btc-updown-5m-"):
        return None

    title = market.get("question") or market.get("title")
    window_start_ts, window_end_ts = _parse_market_window(title, now_ts)
    start_ts = window_start_ts or _parse_iso_ts(market.get("startDate")) or _market_timestamp(market)
    if start_ts <= 0:
        return None

    token_ids = _parse_json_array(market.get("clobTokenIds"))
    outcomes = _parse_json_array(market.get("outcomes"))
    outcome_prices = _parse_json_array(market.get("outcomePrices"))
    token_map = {
        outcome: token_id
        for outcome, token_id in zip(outcomes, token_ids)
    }
    return {
        "event_id": market.get("eventId"),
        "market_id": market.get("id"),
        "condition_id": market.get("conditionId"),
        "slug": market.get("slug"),
        "ticker": market.get("ticker"),
        "title": title,
        "description": market.get("description"),
        "round_start_ts": start_ts,
        "window_start_ts": window_start_ts or start_ts,
        "window_end_ts": window_end_ts or (start_ts + 300),
        "slug_timestamp": _market_timestamp(market),
        "token_ids": token_ids,
        "outcomes": outcomes,
        "outcome_prices": [float(p) for p in outcome_prices] if outcome_prices else [],
        "token_map": token_map,
        "accepting_orders": market.get("acceptingOrders"),
        "enable_order_book": market.get("enableOrderBook"),
        "created_ts": _parse_iso_ts(market.get("createdAt")),
        "updated_ts": _parse_iso_ts(market.get("updatedAt")),
    }


def _freshness_key(candidate: dict) -> tuple[int, int, int]:
    return (
        int(candidate.get("updated_ts") or 0),
        int(candidate.get("created_ts") or 0),
        int(candidate.get("window_start_ts") or candidate.get("round_start_ts") or 0),
    )


def select_btc_5m_market(events: list[dict], now_ts: int | None = None) -> dict | None:
    now_ts = now_ts or int(time.time())
    candidates = []
    for event in events:
        slug = (event.get("slug") or event.get("ticker") or "").lower()
        if not slug.startswith("btc-updown-5m-"):
            continue
        markets = event.get("markets") or []
        if not markets:
            continue
        market = markets[0]
        title = event.get("title") or market.get("question")
        window_start_ts, window_end_ts = _parse_market_window(title, now_ts)
        start_ts = window_start_ts or _parse_iso_ts(market.get("startDate")) or _parse_iso_ts(event.get("startDate")) or _market_timestamp(market) or _market_timestamp(event)
        if start_ts <= 0:
            continue
        token_ids = _parse_json_array(market.get("clobTokenIds"))
        outcomes = _parse_json_array(market.get("outcomes"))
        outcome_prices = _parse_json_array(market.get("outcomePrices"))
        token_map = {
            outcome: token_id
            for outcome, token_id in zip(outcomes, token_ids)
        }
        candidates.append({
            "event_id": event.get("id"),
            "market_id": market.get("id"),
            "condition_id": market.get("conditionId"),
            "slug": market.get("slug") or event.get("slug"),
            "ticker": event.get("ticker"),
            "title": title,
            "description": market.get("description") or event.get("description"),
            "round_start_ts": start_ts,
            "window_start_ts": window_start_ts or start_ts,
            "window_end_ts": window_end_ts or (start_ts + 300),
            "slug_timestamp": _market_timestamp(market) or _market_timestamp(event),
            "token_ids": token_ids,
            "outcomes": outcomes,
            "outcome_prices": [float(p) for p in outcome_prices] if outcome_prices else [],
            "token_map": token_map,
            "accepting_orders": market.get("acceptingOrders"),
            "enable_order_book": market.get("enableOrderBook"),
            "created_ts": _parse_iso_ts(market.get("createdAt")) or _parse_iso_ts(event.get("createdAt")),
            "updated_ts": _parse_iso_ts(market.get("updatedAt")) or _parse_iso_ts(event.get("updatedAt")),
        })

    if not candidates:
        return None

    active = [c for c in candidates if c["window_start_ts"] <= now_ts < c["window_end_ts"]]
    if active:
        active.sort(key=lambda c: c["window_start_ts"], reverse=True)
        return active[0]

    upcoming = [c for c in candidates if 0 <= c["window_start_ts"] - now_ts <= 120]
    if upcoming:
        upcoming.sort(key=lambda c: c["window_start_ts"])
        return upcoming[0]

    freshest_accepting = [
        c for c in candidates
        if c.get("accepting_orders") is True
        and abs(int(c.get("window_start_ts") or c.get("round_start_ts") or 0) - now_ts) <= 1800
    ]
    if freshest_accepting:
        freshest_accepting.sort(key=_freshness_key, reverse=True)
        return freshest_accepting[0]

    pool = [c for c in candidates if now_ts < c["window_end_ts"]] or candidates
    pool.sort(key=lambda c: c["window_start_ts"], reverse=True)
    return pool[0]


def select_btc_5m_market_from_markets(markets: list[dict], now_ts: int | None = None) -> dict | None:
    now_ts = now_ts or int(time.time())
    candidates = []
    for market in markets:
        normalized = _normalize_market_record(market, now_ts)
        if normalized:
            candidates.append(normalized)

    if not candidates:
        return None

    active = [c for c in candidates if c["window_start_ts"] <= now_ts < c["window_end_ts"]]
    if active:
        active.sort(key=lambda c: c["window_start_ts"], reverse=True)
        return active[0]

    upcoming = [c for c in candidates if 0 <= c["window_start_ts"] - now_ts <= 120]
    if upcoming:
        upcoming.sort(key=lambda c: c["window_start_ts"])
        return upcoming[0]

    freshest_accepting = [
        c for c in candidates
        if c.get("accepting_orders") is True
        and abs(int(c.get("window_start_ts") or c.get("round_start_ts") or 0) - now_ts) <= 1800
    ]
    if freshest_accepting:
        freshest_accepting.sort(key=_freshness_key, reverse=True)
        return freshest_accepting[0]

    pool = [c for c in candidates if now_ts < c["window_end_ts"]] or candidates
    pool.sort(key=lambda c: c["window_start_ts"], reverse=True)
    return pool[0]

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
        self._current_poly_market: dict | None = None
        self._poly_books: dict[str, dict] = {}
        self._prediction_lock_seconds = 15

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
        current = self._current_poly_market or {}
        token_map = current.get("token_map", {})
        if not token_map:
            return
        outcome = None
        for name, token_id in token_map.items():
            if token_id == book.get("asset_id"):
                outcome = name
                break
        if outcome is None:
            return

        self._poly_books[outcome] = {**book, "outcome": outcome}
        await self._write_polymarket_books()

    async def _write_polymarket_books(self):
        current = self._current_poly_market or {}
        if not current:
            return
        payload = {
            "market_id": current.get("market_id"),
            "condition_id": current.get("condition_id"),
            "slug": current.get("slug"),
            "title": current.get("title"),
            "token_map": current.get("token_map", {}),
            "books": self._poly_books,
            "updated_at": int(time.time() * 1000),
        }
        atomic_write_json(
            os.path.join(self.data_dir, "live", "polymarket_orderbooks.json"),
            payload,
        )
        up_book = self._poly_books.get("Up")
        if up_book:
            atomic_write_json(
                os.path.join(self.data_dir, "live", "polymarket_orderbook.json"),
                {
                    **up_book,
                    "market_id": current.get("market_id"),
                    "condition_id": current.get("condition_id"),
                    "slug": current.get("slug"),
                    "token_map": current.get("token_map", {}),
                    "books": self._poly_books,
                    "updated_at": int(time.time() * 1000),
                },
            )

    async def _on_new_market(self, market: dict):
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

    async def _fetch_books_for_market(self, market_info: dict):
        token_map = market_info.get("token_map", {})
        if not token_map:
            return
        async with aiohttp.ClientSession() as session:
            for outcome, token_id in token_map.items():
                try:
                    async with session.get(
                        "https://clob.polymarket.com/book",
                        params={"token_id": token_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        resp.raise_for_status()
                        raw = await resp.json()
                        parsed = {
                            "asset_id": raw.get("asset_id"),
                            "market_id": raw.get("market"),
                            "bids": [{"price": float(b["price"]), "size": float(b["size"])} for b in raw.get("bids", [])],
                            "asks": [{"price": float(a["price"]), "size": float(a["size"])} for a in raw.get("asks", [])],
                            "timestamp": raw.get("timestamp"),
                            "last_trade_price": float(raw.get("last_trade_price", 0) or 0),
                            "outcome": outcome,
                        }
                        self._poly_books[outcome] = parsed
                except Exception as e:
                    logger.warning(f"CLOB book fetch failed for {outcome} token {token_id[:12]}...: {e}")
        await self._write_polymarket_books()

    def _market_is_tradeable(self, market_info: dict, now_ts: float | None = None) -> bool:
        if market_info.get("accepting_orders") is True:
            return True
        if market_info.get("accepting_orders") is False:
            return False
        window_start_ts = int(market_info.get("window_start_ts") or market_info.get("round_start_ts") or 0)
        window_end_ts = int(market_info.get("window_end_ts") or 0)
        if window_start_ts <= 0:
            return False
        now_ts = time.time() if now_ts is None else now_ts
        lock_at = (window_end_ts if window_end_ts > 0 else (window_start_ts + 300)) - self._prediction_lock_seconds
        return now_ts < lock_at

    async def _discover_btc_market(self) -> dict | None:
        """Discover the current BTC 5m Polymarket market and token IDs."""
        try:
            async with aiohttp.ClientSession() as session:
                market_info = None
                now_ts = int(time.time())

                markets_url = f"{GAMMA_API}/markets"
                for slug in _candidate_btc_5m_slugs(now_ts):
                    try:
                        async with session.get(
                            f"{markets_url}/slug/{slug}",
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                raw_market = await resp.json()
                                market_info = _normalize_market_record(raw_market, now_ts)
                                if market_info:
                                    market_info["discovery_source"] = "markets_slug"
                                    break
                        if market_info is not None:
                            break
                        async with session.get(
                            markets_url,
                            params={"slug": slug, "limit": "5"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            raw_markets = await resp.json()
                    except Exception:
                        continue
                    market_info = select_btc_5m_market_from_markets(raw_markets, now_ts=now_ts)
                    if market_info:
                        market_info["discovery_source"] = "markets_slug_query"
                        break

                if market_info is None:
                    markets_params = {
                        "active": "true",
                        "closed": "false",
                        "limit": "200",
                        "order": "startDate",
                        "ascending": "false",
                    }
                    async with session.get(markets_url, params=markets_params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        markets = await resp.json()
                        market_info = select_btc_5m_market_from_markets(markets, now_ts=now_ts)
                        if market_info:
                            market_info["discovery_source"] = "markets"

                if market_info is None:
                    events_url = f"{GAMMA_API}/events"
                    events_params = {
                        "active": "true",
                        "closed": "false",
                        "limit": "200",
                        "order": "startDate",
                        "ascending": "false",
                    }
                    async with session.get(events_url, params=events_params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        events = await resp.json()
                        market_info = select_btc_5m_market(events, now_ts=now_ts)
                        if market_info:
                            market_info["discovery_source"] = "events"

                if not market_info:
                    logger.warning("No active BTC 5m market found in Gamma markets/events")
                    return None

                atomic_write_json(
                    os.path.join(self.data_dir, "live", "polymarket_market.json"),
                    {
                        **market_info,
                        "updated_at": int(time.time() * 1000),
                    },
                )
                logger.info(
                    f"Discovered BTC 5m market {market_info['slug']} "
                    f"with {len(market_info['token_ids'])} tokens"
                )
                return market_info
        except Exception as e:
            logger.warning(f"Market discovery failed: {e}")
            return None

    async def _market_discovery_loop(self, poly_market: PolymarketMarketWS):
        """Periodically discover the current BTC market, subscribe to its token IDs, and align rounds."""
        known_tokens: set[str] = set()
        last_market_slug: str | None = None
        while self._running:
            try:
                market_info = await self._discover_btc_market()
                if market_info:
                    if not self._market_is_tradeable(market_info):
                        logger.info(
                            f"Skipping stale BTC market {market_info['slug']} "
                            f"(round_start_ts={market_info['round_start_ts']})"
                        )
                        await asyncio.sleep(10)
                        continue
                    self._current_poly_market = market_info
                    new_tokens = [t for t in market_info["token_ids"] if t not in known_tokens]
                    if new_tokens:
                        await poly_market.subscribe(new_tokens)
                        known_tokens.update(new_tokens)
                        logger.info(f"Subscribed to {len(new_tokens)} BTC market tokens")

                    if market_info["slug"] != last_market_slug:
                        last_market_slug = market_info["slug"]
                        self._poly_books = {}
                        round_timestamp = market_info["round_start_ts"]
                        if self.round_manager.current_round != round_timestamp:
                            self.round_manager.freeze_snapshot(round_timestamp)
                            self._last_round_time = time.time()
                            logger.info(
                                f"=== POLYMARKET ROUND {round_timestamp} === "
                                f"{market_info['slug']}"
                            )
                    # Keep REST book refresh running even if the websocket is noisy.
                    await self._fetch_books_for_market(market_info)
                else:
                    logger.warning("BTC market discovery returned no market")
            except Exception as e:
                logger.warning(f"Market discovery loop error: {e}")
            await asyncio.sleep(2)

    async def _heartbeat_loop(self):
        while self._running:
            self.write_heartbeat()
            await asyncio.sleep(5)

    async def run(self, config: Config | None = None):
        self.init_dirs()
        self._running = True
        if config is not None:
            self._prediction_lock_seconds = config.prediction_lock_seconds
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
        market_info = await self._discover_btc_market()
        initial_asset_ids = market_info["token_ids"] if market_info and self._market_is_tradeable(market_info) else []
        if market_info and self._market_is_tradeable(market_info):
            self._current_poly_market = market_info
            await self._fetch_books_for_market(market_info)
            self.round_manager.freeze_snapshot(market_info["round_start_ts"])
            self._last_round_time = time.time()
            logger.info(f"=== POLYMARKET ROUND {market_info['round_start_ts']} === {market_info['slug']}")
        elif market_info:
            logger.info(
                f"Skipping stale BTC market on startup: {market_info['slug']} "
                f"(round_start_ts={market_info['round_start_ts']})"
            )

        logger.info("Collector starting connections...")

        tasks = [
            asyncio.create_task(binance.connect()),
            asyncio.create_task(poly_market.connect(initial_asset_ids=initial_asset_ids)),
            asyncio.create_task(poly_rtds.connect()),
            asyncio.create_task(poller.run()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._market_discovery_loop(poly_market)),
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
