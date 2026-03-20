import json
import asyncio
import time
import aiohttp

DEFAULT_ORDER_SIZE_SHARES = 100.0
PRICE_API = "https://clob.polymarket.com/price"


def _parse_json_array(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return []


def _normalize_levels(levels: list[dict], *, side: str) -> list[dict]:
    price_key = "price"
    size_key = "size"
    normalized = []
    for level in levels or []:
        price = level.get(price_key)
        size = level.get(size_key)
        if price is None or size is None:
            continue
        normalized.append({
            "price": float(price),
            "size": float(size),
        })
    normalized.sort(key=lambda level: level["price"], reverse=(side == "bids"))
    return normalized


def _best_price(levels: list[dict], *, side: str) -> float | None:
    normalized = _normalize_levels(levels, side=side)
    if not normalized:
        return None
    return normalized[0]["price"]


def _estimate_buy_fill(levels: list[dict], shares: float) -> dict | None:
    if shares <= 0:
        return None
    asks = _normalize_levels(levels, side="asks")
    if not asks:
        return None

    remaining = float(shares)
    total_cost = 0.0
    filled = 0.0
    levels_used = 0
    for level in asks:
        if remaining <= 0:
            break
        take = min(remaining, level["size"])
        if take <= 0:
            continue
        total_cost += take * level["price"]
        filled += take
        remaining -= take
        levels_used += 1

    if filled <= 0 or remaining > 1e-9:
        return None

    return {
        "shares": filled,
        "total_cost": total_cost,
        "vwap_price": total_cost / filled,
        "levels_used": levels_used,
    }


def _extract_market_info(snapshot: dict) -> dict:
    market = snapshot.get("polymarket_market", {}) or {}
    token_ids = market.get("token_ids") or _parse_json_array(market.get("token_ids"))
    outcomes = market.get("outcomes") or _parse_json_array(market.get("outcomes"))
    token_map = market.get("token_map") or {
        outcome: token_id
        for outcome, token_id in zip(outcomes, token_ids)
    }
    return {
        "market_id": market.get("market_id"),
        "slug": market.get("slug"),
        "token_map": token_map,
        "outcomes": outcomes,
    }


def build_execution_quote(snapshot: dict, prediction: str) -> dict | None:
    market_info = _extract_market_info(snapshot)
    if prediction not in {"Up", "Down"}:
        return None

    books_payload = snapshot.get("polymarket_orderbooks", {}) or {}
    books = books_payload.get("books") if isinstance(books_payload, dict) else None
    if not books:
        up_book = snapshot.get("polymarket_orderbook")
        books = {"Up": up_book} if up_book else {}

    book = books.get(prediction)
    if not isinstance(book, dict):
        return None

    shares = DEFAULT_ORDER_SIZE_SHARES
    fill = _estimate_buy_fill(book.get("asks", []), shares)
    best_ask = _best_price(book.get("asks", []), side="asks")
    best_bid = _best_price(book.get("bids", []), side="bids")
    last_trade = book.get("last_trade_price")
    if fill is None or best_ask is None:
        return None

    return {
        "market_id": market_info.get("market_id"),
        "market_slug": market_info.get("slug"),
        "asset_id": market_info.get("token_map", {}).get(prediction),
        "predicted_outcome": prediction,
        "entry_price": float(fill["vwap_price"]),
        "order_size_shares": float(fill["shares"]),
        "order_total_cost": float(fill["total_cost"]),
        "levels_crossed": int(fill["levels_used"]),
        "best_ask": float(best_ask),
        "best_bid": float(best_bid) if best_bid is not None else None,
        "last_trade_price": float(last_trade) if last_trade is not None else None,
        "entry_price_source": "polymarket_book_walk_buy",
        "quote_used_at": int(time.time() * 1000),
    }


async def _fetch_market_price(session: aiohttp.ClientSession, token_id: str, side: str) -> float | None:
    for attempt in range(3):
        try:
            async with session.get(
                PRICE_API,
                params={"token_id": token_id, "side": side},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
            price = payload.get("price")
            if price is None:
                raise ValueError("missing price")
            return float(price)
        except Exception:
            if attempt == 2:
                return None
            await asyncio.sleep(0.15 * (attempt + 1))
    return None


async def build_live_execution_quote(
    snapshot: dict,
    prediction: str,
    session: aiohttp.ClientSession | None = None,
) -> dict | None:
    market_info = _extract_market_info(snapshot)
    if prediction not in {"Up", "Down"}:
        return None

    asset_id = market_info.get("token_map", {}).get(prediction)
    if not asset_id:
        return build_execution_quote(snapshot, prediction)

    books_payload = snapshot.get("polymarket_orderbooks", {}) or {}
    books = books_payload.get("books") if isinstance(books_payload, dict) else None
    if not books:
        up_book = snapshot.get("polymarket_orderbook")
        books = {"Up": up_book} if up_book else {}
    book = books.get(prediction) or {}

    best_ask = _best_price(book.get("asks", []), side="asks")
    best_bid = _best_price(book.get("bids", []), side="bids")
    last_trade = book.get("last_trade_price")

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        # Per Polymarket docs, BUY returns best bid and SELL returns best ask.
        price_bid = await _fetch_market_price(session, asset_id, "BUY")
        price_ask = await _fetch_market_price(session, asset_id, "SELL")
    finally:
        if owns_session:
            await session.close()

    # Our paper trade is a marketable buy of the predicted outcome token,
    # so the closest executable quote is the best ask from /price (SELL side).
    entry_price = price_ask
    entry_source = "polymarket_price_sell"
    if entry_price is None:
        fallback = build_execution_quote(snapshot, prediction)
        if fallback is None:
            return None
        fallback.update({
            "price_endpoint_bid": price_bid,
            "price_endpoint_ask": price_ask,
            "price_endpoint_source": "unavailable",
        })
        return fallback

    quote = {
        "market_id": market_info.get("market_id"),
        "market_slug": market_info.get("slug"),
        "asset_id": asset_id,
        "predicted_outcome": prediction,
        "entry_price": float(entry_price),
        "entry_price_source": entry_source,
        "quote_used_at": int(time.time() * 1000),
        "price_endpoint_bid": float(price_bid) if price_bid is not None else None,
        "price_endpoint_ask": float(price_ask) if price_ask is not None else None,
        "best_ask": float(best_ask) if best_ask is not None else None,
        "best_bid": float(best_bid) if best_bid is not None else None,
        "last_trade_price": float(last_trade) if last_trade is not None else None,
        "order_size_shares": DEFAULT_ORDER_SIZE_SHARES,
        "order_total_cost": float(entry_price) * DEFAULT_ORDER_SIZE_SHARES,
        "levels_crossed": 1,
    }
    if best_ask is not None:
        quote["price_vs_book_ask_diff"] = float(entry_price) - float(best_ask)
    if best_bid is not None:
        quote["price_vs_book_bid_diff"] = float(entry_price) - float(best_bid)
    return quote


def score_execution(record: dict, outcome: str) -> dict | None:
    entry_price = record.get("entry_price")
    prediction = record.get("prediction")
    if entry_price in (None, 0) or prediction not in {"Up", "Down"}:
        return None

    payout = 1.0 if prediction == outcome else 0.0
    pnl_per_share = payout - float(entry_price)
    return_pct = pnl_per_share / float(entry_price)
    return {
        "entry_price": float(entry_price),
        "payout": payout,
        "pnl_per_share": pnl_per_share,
        "return_pct": return_pct,
        "outcome": outcome,
    }
