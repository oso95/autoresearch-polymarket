# src/main.py
import argparse
import asyncio
import json
import logging
import os
import signal
import time

import aiohttp

from src.config import Config, load_config
from src.data_layer.collector import Collector
from src.data_layer.retention import RetentionManager
from src.coordinator.tournament import Tournament
from src.coordinator.spawner import AgentSpawner
from src.runner.agent_runner import AgentRunner, build_agent_context
from src.runner.health_monitor import HealthMonitor
from src.runner.predictor import Predictor
from src.runner.fast_fail import FastFailChecker
from src.runner.evolver import StrategyEvolver
from src.runner.paper_execution import build_execution_quote, build_live_execution_quote
from src.io_utils import read_jsonl, read_jsonl_tail
from src.memory_utils import read_memory_bundle
from src.runner.decision_tracker import build_decision_context_for_agent
from src.runner.outcome_analyzer import build_outcome_context
from src.shared_knowledge import build_shared_knowledge_context, ensure_shared_knowledge_forum

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
ROUND_DURATION_SECONDS = 300
ROUND_FUTURE_TOLERANCE_SECONDS = 60


def _round_is_tradeable(round_timestamp: int | None, prediction_lock_seconds: int, now: float | None = None) -> bool:
    if round_timestamp is None:
        return False
    now = time.time() if now is None else now
    lock_at = round_timestamp + ROUND_DURATION_SECONDS - prediction_lock_seconds
    return now < lock_at


def _load_live_market(data_dir: str) -> dict | None:
    market_path = os.path.join(data_dir, "live", "polymarket_market.json")
    if not os.path.exists(market_path):
        return None
    try:
        with open(market_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _round_matches_accepting_market(data_dir: str, round_timestamp: int | None) -> bool:
    if round_timestamp is None:
        return False
    market = _load_live_market(data_dir)
    if not market:
        return False
    market_round = market.get("round_start_ts") or market.get("window_start_ts")
    return (
        int(market_round or 0) == int(round_timestamp)
        and market.get("accepting_orders") is True
    )


def _best_level(levels: list[dict], price_key: str, size_key: str, *, best: str) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    ordered = sorted(
        (
            (float(level.get(price_key)), float(level.get(size_key, 0) or 0))
            for level in levels
            if level.get(price_key) is not None
        ),
        key=lambda item: item[0],
        reverse=(best == "bid"),
    )
    return ordered[0] if ordered else (None, None)


def _sum_sizes(levels: list[dict], size_key: str, limit: int = 5) -> float:
    return sum(float(level.get(size_key, 0) or 0) for level in levels[:limit])


def _build_live_features(snapshot: dict) -> dict:
    features: dict[str, float | int | None] = {}

    orderbook = snapshot.get("binance_orderbook") or {}
    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []
    best_bid, best_bid_qty = _best_level(bids, "price", "qty", best="bid")
    best_ask, best_ask_qty = _best_level(asks, "price", "qty", best="ask")
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        depth_bid = _sum_sizes(bids, "qty")
        depth_ask = _sum_sizes(asks, "qty")
        depth_total = depth_bid + depth_ask
        features.update({
            "binance_best_bid": best_bid,
            "binance_best_ask": best_ask,
            "binance_mid_price": mid,
            "binance_spread": spread,
            "binance_spread_bps": (spread / mid * 10000) if mid else 0.0,
            "binance_top_bid_qty": best_bid_qty,
            "binance_top_ask_qty": best_ask_qty,
            "binance_depth_bid_qty_5": depth_bid,
            "binance_depth_ask_qty_5": depth_ask,
            "binance_depth_imbalance_5": ((depth_bid - depth_ask) / depth_total) if depth_total else 0.0,
        })

    trades = (snapshot.get("binance_trades_recent") or {}).get("trades") or []
    if trades:
        recent = trades[-100:]
        first_price = float(recent[0].get("p", 0) or 0)
        last_price = float(recent[-1].get("p", 0) or 0)
        buy_volume = 0.0
        sell_volume = 0.0
        for trade in recent:
            qty = float(trade.get("q", 0) or 0)
            if trade.get("m"):
                sell_volume += qty
            else:
                buy_volume += qty
        total_volume = buy_volume + sell_volume
        features.update({
            "recent_trade_count_100": len(recent),
            "recent_trade_first_price": first_price,
            "recent_trade_last_price": last_price,
            "recent_trade_return_bps_100": (((last_price - first_price) / first_price) * 10000) if first_price else 0.0,
            "recent_buy_volume_100": buy_volume,
            "recent_sell_volume_100": sell_volume,
            "recent_signed_volume_100": buy_volume - sell_volume,
            "recent_trade_imbalance_100": ((buy_volume - sell_volume) / total_volume) if total_volume else 0.0,
        })

    poly_books = ((snapshot.get("polymarket_orderbooks") or {}).get("books") or {})
    for outcome in ("Up", "Down"):
        book = poly_books.get(outcome) or {}
        bid, bid_size = _best_level(book.get("bids") or [], "price", "size", best="bid")
        ask, ask_size = _best_level(book.get("asks") or [], "price", "size", best="ask")
        prefix = f"polymarket_{outcome.lower()}"
        if bid is not None:
            features[f"{prefix}_best_bid"] = bid
            features[f"{prefix}_best_bid_size"] = bid_size
        if ask is not None:
            features[f"{prefix}_best_ask"] = ask
            features[f"{prefix}_best_ask_size"] = ask_size
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
            features[f"{prefix}_mid"] = mid
            features[f"{prefix}_spread"] = ask - bid
    if features.get("polymarket_up_mid") is not None and features.get("polymarket_down_mid") is not None:
        features["polymarket_mid_skew"] = features["polymarket_up_mid"] - features["polymarket_down_mid"]

    return features


def init_project(project_dir: str):
    os.makedirs(project_dir, exist_ok=True)
    data_dir = os.path.join(project_dir, "data")
    agents_dir = os.path.join(project_dir, "agents")
    for subdir in ["live", "polling", "history", "rounds", "coordinator", "archive", "shared_knowledge"]:
        os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
    os.makedirs(agents_dir, exist_ok=True)

    # Seed shared knowledge directory with initial guidance
    shared_dir = os.path.join(data_dir, "shared_knowledge")
    ensure_shared_knowledge_forum(shared_dir)
    guidance_path = os.path.join(shared_dir, "approaches.md")
    if not os.path.exists(guidance_path):
        with open(guidance_path, "w") as f:
            f.write("""# Strategy Approaches

You are free to use ANY approach to predict BTC 5-minute direction. Some ideas:

## Traditional Finance
- Technical analysis (RSI, MACD, Bollinger Bands, moving averages)
- Order book microstructure (imbalance, depth, spread dynamics)
- Volume profile analysis, VWAP
- Funding rate arbitrage signals
- Open interest divergence

## Alternative / Unconventional
- Yi Jing (I Ching) hexagram casting based on market data as seed
- Tarot-inspired pattern mapping (market archetypes)
- Numerology patterns in price/volume digits
- Astrological correlations (lunar cycles, planetary alignments)
- Fibonacci sequences in price action
- Fractal analysis and self-similarity detection

## Statistical / ML-Inspired
- Mean reversion with dynamic thresholds
- Regime detection (trending vs ranging vs volatile)
- Cross-correlation between Polymarket odds and actual outcomes
- Bayesian probability updating
- Markov chain state transitions

## Meta-Strategies
- Contrarian: fade extreme consensus
- Follow the smart money: track top trader positioning
- Time-of-day patterns (certain hours more predictable)
- Volatility regime switching

The best approach is the one that WORKS. Win rate is all that matters.
Don't be afraid to try something unconventional — the tournament will
keep what works and discard what doesn't.
""")
    forum_guide_path = os.path.join(shared_dir, "forum-guide.md")
    if not os.path.exists(forum_guide_path):
        with open(forum_guide_path, "w") as f:
            f.write("""# Shared Knowledge Forum Guide

Use the shared knowledge forum like a small Stack Overflow for agents.

## When To Create A Post
- You found a pattern that may help other agents
- You validated or invalidated a threshold, rule, or signal
- You discovered a regime-specific failure mode
- You found a reusable script idea or data feature

## Good Post Structure
- Title: short and specific
- Claim: what you think is true
- Evidence: what rounds or patterns support it
- Caveat: when it may fail

## Voting
- Upvote posts that match your own evidence
- Downvote posts that seem contradicted by your results
- Add a short reason when voting if possible

## Comments
- Leave short comments with confirmations, caveats, or counterexamples
- Prefer concise evidence over long essays

## Keep It High Signal
- Do not repost the same idea repeatedly
- Do not dump raw logs
- Prefer specific, testable insights
""")
    config_path = os.path.join(project_dir, "config.json")
    if not os.path.exists(config_path):
        config = Config()
        with open(config_path, "w") as f:
            json.dump({k: v for k, v in config.__dict__.items()}, f, indent=2)


async def _invoke_single_agent(
    agent_name: str,
    agents_dir: str,
    snapshot: dict,
    predictor: Predictor,
    runner: AgentRunner,
    round_timestamp: int,
    shared_knowledge_dir: str,
    price_session: aiohttp.ClientSession | None = None,
):
    """Invoke a single agent for prediction. Designed to run in parallel."""
    agent_dir = os.path.join(agents_dir, agent_name)

    strategy_path = os.path.join(agent_dir, "strategy.md")
    if not os.path.exists(strategy_path):
        return
    with open(strategy_path) as f:
        strategy = f.read()

    scripts = {}
    scripts_dir = os.path.join(agent_dir, "scripts")
    if os.path.isdir(scripts_dir):
        for fname in os.listdir(scripts_dir):
            fpath = os.path.join(scripts_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath) as f:
                    scripts[fname] = f.read()

    notes = read_memory_bundle(agent_dir)

    # Add shared knowledge (core files + last 10 discoveries to save tokens)
    if os.path.isdir(shared_knowledge_dir):
        shared_context = build_shared_knowledge_context(shared_knowledge_dir, agent_name)
        if shared_context.strip():
            notes += "\n\n" + shared_context

    # Add decision quality profile (revision accuracy, flip analysis)
    decision_context = build_decision_context_for_agent(agents_dir, agent_name)
    if decision_context:
        notes += "\n\n" + decision_context

    # Add outcome pattern analysis (autocorrelation, time-of-day patterns)
    data_dir_for_outcome = os.path.dirname(shared_knowledge_dir)  # data_dir
    outcome_context = build_outcome_context(data_dir_for_outcome)
    if outcome_context:
        notes += "\n\n" + outcome_context

    # Recent results summary (use tail read for efficiency — only need last 20)
    preds = read_jsonl_tail(os.path.join(agent_dir, "predictions.jsonl"), 20)
    scored = [p for p in preds if p.get("correct") is not None]
    recent = scored[-10:]
    recent_str = ", ".join("W" if p["correct"] else "L" for p in recent) if recent else "no history"

    # Read agent_config.json once (for model override + mirror flag)
    agent_config = {}
    agent_config_path = os.path.join(agent_dir, "agent_config.json")
    if os.path.exists(agent_config_path):
        with open(agent_config_path) as f:
            agent_config = json.load(f)

    result = await predictor.get_prediction(
        agent_dir=agent_dir,
        strategy=strategy,
        snapshot=snapshot,
        scripts=scripts,
        recent_results=recent_str,
        notes=notes,
        model=agent_config.get("model"),
    )

    if result is None:
        logger.warning(f"  {agent_name}: timeout/error, skipping")
        return

    # Mirror agent support: flip signal
    if agent_config.get("mirror"):
        original = result["prediction"]
        result["prediction"] = "Down" if original == "Up" else "Up"
        result["reasoning"] = f"[MIRROR of {original}] {result['reasoning']}"

    strategy_version = str(hash(strategy))[:8]
    execution_quote = await build_live_execution_quote(snapshot, result["prediction"], session=price_session)
    if execution_quote is None:
        execution_quote = build_execution_quote(snapshot, result["prediction"])

    runner.record_prediction(
        agent_name, round_timestamp,
        result["prediction"], result["confidence"],
        result["reasoning"], strategy_version,
        execution_quote=execution_quote,
    )
    price_bits = []
    if execution_quote:
        if execution_quote.get("entry_price") is not None:
            price_bits.append(f"entry {float(execution_quote['entry_price']):.2f}")
        if execution_quote.get("entry_price_source"):
            price_bits.append(str(execution_quote["entry_price_source"]))
        if execution_quote.get("quote_used_at"):
            price_bits.append(f"quote_at {execution_quote['quote_used_at']}")
    price_suffix = f" [{' | '.join(price_bits)}]" if price_bits else ""
    logger.info(
        f"  {agent_name}: {result['prediction']} (confidence {result['confidence']:.0%})"
        f"{price_suffix} — {result['reasoning'][:80]}"
    )


async def _run_round(
    round_timestamp: int,
    data_dir: str,
    agents_dir: str,
    runner: AgentRunner,
    predictor: Predictor,
    fast_fail: FastFailChecker,
    config: Config,
):
    """Invoke ALL agents in PARALLEL for the current state of a round."""
    snapshot = _build_live_round_snapshot(data_dir, round_timestamp)
    if snapshot is None:
        logger.warning(f"No live snapshot available for round {round_timestamp}, skipping")
        return

    _trim_prediction_snapshot(snapshot)
    open_snapshot = snapshot.get("round_open_snapshot")
    if isinstance(open_snapshot, dict):
        _trim_prediction_snapshot(open_snapshot)

    agents = runner.discover_agents()
    shared_knowledge_dir = os.path.join(data_dir, "shared_knowledge")
    age_seconds = snapshot.get("round_context", {}).get("age_seconds")
    logger.info(
        f"Round {round_timestamp}: updating {len(agents)} agents "
        f"(age={age_seconds}s, max 8 concurrent)"
    )

    # Use semaphore to limit concurrent Codex/GPT calls.
    sem = asyncio.Semaphore(8)  # Increased for larger agent pool (38+ agents)

    async with aiohttp.ClientSession() as price_session:
        async def _limited_invoke(agent_name):
            async with sem:
                return await _invoke_single_agent(
                    agent_name, agents_dir, snapshot, predictor,
                    runner, round_timestamp, shared_knowledge_dir,
                    price_session=price_session,
                )

        tasks = [_limited_invoke(name) for name in agents]
        await asyncio.gather(*tasks, return_exceptions=True)


def _trim_prediction_snapshot(snapshot: dict) -> None:
    # Trim snapshot to reduce token usage — agents don't need 100 candles
    if "binance_candles_5m" in snapshot:
        candles = snapshot["binance_candles_5m"].get("candles", [])
        snapshot["binance_candles_5m"]["candles"] = candles[-20:]  # Last 20 only
    # Trim trades to last 50
    if "binance_trades_recent" in snapshot:
        trades = snapshot["binance_trades_recent"].get("trades", [])
        snapshot["binance_trades_recent"]["trades"] = trades[-50:]
    # Trim polling data to last 5 records each
    if "polling" in snapshot and isinstance(snapshot["polling"], dict):
        for key, val in snapshot["polling"].items():
            if isinstance(val, dict) and "data" in val:
                val["data"] = val["data"][-5:]

def _build_live_round_snapshot(data_dir: str, round_timestamp: int) -> dict | None:
    round_snapshot_path = os.path.join(data_dir, "rounds", str(round_timestamp), "snapshot.json")
    if not os.path.exists(round_snapshot_path):
        return None

    with open(round_snapshot_path) as f:
        round_open_snapshot = json.load(f)

    snapshot = {}
    live_dir = os.path.join(data_dir, "live")
    for fname in os.listdir(live_dir):
        if fname.endswith(".json") and fname not in {"status.json", "heartbeat.json"}:
            key = fname.replace(".json", "")
            with open(os.path.join(live_dir, fname)) as f:
                snapshot[key] = json.load(f)

    polling = {}
    polling_dir = os.path.join(data_dir, "polling")
    if os.path.isdir(polling_dir):
        for fname in os.listdir(polling_dir):
            if fname.endswith(".json"):
                key = fname.replace(".json", "")
                with open(os.path.join(polling_dir, fname)) as f:
                    polling[key] = json.load(f)

    now = time.time()
    snapshot["polling"] = polling
    snapshot["round_timestamp"] = round_timestamp
    snapshot["round_open_snapshot"] = round_open_snapshot
    snapshot["live_features"] = _build_live_features(snapshot)
    snapshot["round_context"] = {
        "round_timestamp": round_timestamp,
        "age_seconds": max(0, int(now - round_timestamp)),
        "seconds_remaining": max(0, int((round_timestamp + ROUND_DURATION_SECONDS) - now)),
        "opened_at": round_open_snapshot.get("frozen_at"),
        "snapshot_path": round_snapshot_path,
    }
    snapshot["frozen_at"] = round_open_snapshot.get("frozen_at")
    return snapshot


def _get_current_round_timestamp(data_dir: str) -> int | None:
    current_round_path = os.path.join(data_dir, "live", "current-round.json")
    if not os.path.exists(current_round_path):
        return None
    try:
        with open(current_round_path) as f:
            current = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    round_timestamp = current.get("round_timestamp")
    return int(round_timestamp) if isinstance(round_timestamp, (int, float)) else None


def _determine_outcome_from_candles(data_dir: str, round_timestamp: int) -> str | None:
    """Determine Up/Down from Binance candle data.

    Compare the BTC price at round start (from snapshot) to the current
    latest candle close. If price went up → "Up", down → "Down".
    This mirrors Polymarket's resolution (Chainlink tracks Binance closely).
    """
    # Get the snapshot's price
    snapshot_path = os.path.join(data_dir, "rounds", str(round_timestamp), "snapshot.json")
    if not os.path.exists(snapshot_path):
        return None
    try:
        with open(snapshot_path) as f:
            snapshot = json.load(f)
        candles = snapshot.get("binance_candles_5m", {}).get("candles", [])
        if not candles:
            return None
        open_price = candles[-1]["close"]  # Last candle close = this round's "open"
    except (KeyError, IndexError, json.JSONDecodeError):
        return None

    # Get current price from live candles
    candles_path = os.path.join(data_dir, "live", "binance_candles_5m.json")
    if not os.path.exists(candles_path):
        return None
    try:
        with open(candles_path) as f:
            live = json.load(f)
        live_candles = live.get("candles", [])
        if not live_candles:
            return None
        close_price = live_candles[-1]["close"]
    except (KeyError, IndexError, json.JSONDecodeError):
        return None

    return "Up" if close_price >= open_price else "Down"


async def _score_round(round_timestamp: int, data_dir: str, runner: AgentRunner, fast_fail: FastFailChecker, agents_dir: str):
    """Score all agents for a resolved round."""
    # Try official resolution first
    result_path = os.path.join(data_dir, "rounds", str(round_timestamp), "result.json")
    outcome = None

    if os.path.exists(result_path):
        with open(result_path) as f:
            result = json.load(f)
        outcome = result.get("outcome")

    # Fall back to candle-based determination
    if not outcome:
        outcome = _determine_outcome_from_candles(data_dir, round_timestamp)

    if not outcome:
        logger.warning(f"Cannot determine outcome for round {round_timestamp}, skipping scoring")
        return

    # Write result.json if it didn't exist
    if not os.path.exists(result_path):
        from src.io_utils import atomic_write_json
        atomic_write_json(result_path, {
            "round_timestamp": round_timestamp,
            "outcome": outcome,
            "source": "candle_derived",
            "resolved_at": int(time.time() * 1000),
        })

    agents = runner.discover_agents()
    scored_count = 0
    correct_count = 0

    for agent_name in agents:
        # Use read_jsonl_tail to efficiently check recent predictions
        # (the prediction for this round will be near the end of the file)
        pred_path = os.path.join(agents_dir, agent_name, "predictions.jsonl")
        recent_preds = read_jsonl_tail(pred_path, 5)
        has_prediction = any(p["round"] == round_timestamp and p.get("outcome") is None for p in recent_preds)
        if not has_prediction:
            continue

        runner.score_round(agent_name, round_timestamp, outcome)
        scored_count += 1

        # Check correctness from the same data (score_round writes it back)
        scored_preds = read_jsonl_tail(pred_path, 5)
        for p in scored_preds:
            if p["round"] == round_timestamp and p.get("correct") is not None:
                if p["correct"]:
                    correct_count += 1
                break

        # Check fast-fail
        agent_dir = os.path.join(agents_dir, agent_name)
        if fast_fail.should_revert(agent_dir):
            fast_fail.revert_strategy(agent_dir)
            logger.info(f"  {agent_name}: FAST-FAIL triggered, reverted strategy")

    logger.info(f"Round {round_timestamp} scored: outcome={outcome}, {correct_count}/{scored_count} correct")


async def _evolve_single_agent(evolver: StrategyEvolver, agent_name: str, agents_dir: str):
    """Evolve a single agent. Designed to run with limited concurrency."""
    agent_dir = os.path.join(agents_dir, agent_name)

    # Skip mirror agents — their strategy must stay frozen to test the mirror hypothesis
    config_path = os.path.join(agent_dir, "agent_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            agent_config = json.load(f)
        if agent_config.get("mirror"):
            return

    # Skip ensemble agents — they're voting mechanisms, not individual strategies
    if "ensemble" in agent_name:
        return

    preds = read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
    scored = [p for p in preds if p.get("correct") is not None]
    if len(scored) < 3:
        return

    finalized = evolver.finalize_pending_experiment(agent_name)
    if finalized and finalized.get("status") == "pending":
        logger.info(f"  {agent_name}: pending experiment still gathering rounds ({finalized.get('rounds', 0)}/{evolver.evaluation_window})")
        return
    if finalized and finalized.get("status") in {"keep", "discard"}:
        logger.info(f"  {agent_name}: finalized experiment -> {finalized['status']} ({finalized.get('win_rate', 0):.1%})")

    logger.info(f"  Evolving {agent_name}...")
    result = await evolver.evolve_agent(agent_name)
    if result:
        evolver.apply_evolution(agent_name, result)
        change = result.get("change_description", "unknown")
        logger.info(f"  {agent_name} evolved: {change}")
    else:
        logger.warning(f"  {agent_name} evolution failed (will retry next window)")


async def _evolve_agents(evolver: StrategyEvolver, runner: AgentRunner, fast_fail: FastFailChecker, agents_dir: str):
    """Run the autoresearch inner loop: evolve agents (2 concurrent to save time)."""
    agents = runner.discover_agents()
    sem = asyncio.Semaphore(3)  # 3 concurrent Codex/GPT evolutions

    async def _limited(name):
        async with sem:
            return await _evolve_single_agent(evolver, name, agents_dir)

    tasks = [_limited(name) for name in agents]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _monitor_round_until_lock(
    round_timestamp: int,
    data_dir: str,
    agents_dir: str,
    runner: AgentRunner,
    predictor: Predictor,
    fast_fail: FastFailChecker,
    config: Config,
    monitor: HealthMonitor,
    stop_event: asyncio.Event,
):
    interval = max(5, config.intraround_update_interval_seconds)
    cycle = 0

    logger.info(
        f"Round {round_timestamp}: continuous monitoring started "
        f"(interval={interval}s, lock={config.prediction_lock_seconds}s before close)"
    )

    while not stop_event.is_set():
        now = time.time()
        market_accepting = _round_matches_accepting_market(data_dir, round_timestamp)
        lock_at = round_timestamp + ROUND_DURATION_SECONDS - config.prediction_lock_seconds
        if not market_accepting and now >= lock_at:
            break
        remaining_to_lock = max(0.0, lock_at - now)
        cycle_interval = 1 if remaining_to_lock <= 15 else interval

        if not monitor.is_data_layer_healthy():
            logger.warning(f"Data layer unhealthy during round {round_timestamp}, skipping update cycle")
        else:
            cycle += 1
            logger.info(f"Round {round_timestamp}: prediction cycle {cycle}")
            await _run_round(
                round_timestamp, data_dir, agents_dir,
                runner, predictor, fast_fail, config,
            )

        if _round_matches_accepting_market(data_dir, round_timestamp):
            remaining = cycle_interval
        else:
            remaining = lock_at - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(cycle_interval, remaining))

    logger.info(f"Round {round_timestamp}: prediction lock reached, waiting for resolution")


async def _orchestration_loop(
    data_dir: str,
    agents_dir: str,
    config: Config,
    runner: AgentRunner,
    predictor: Predictor,
    fast_fail: FastFailChecker,
    evolver: StrategyEvolver,
    tournament: Tournament,
    retention: RetentionManager,
    monitor: HealthMonitor,
    stop_event: asyncio.Event,
):
    """Main orchestration: detect rounds, invoke agents, score, evolve, run tournament."""
    last_round: int | None = None
    last_evolution_round_count = 0
    last_tournament_round_count = 0
    rounds_since_cleanup = 0
    evolution_task: asyncio.Task | None = None
    active_round_tasks: dict[int, asyncio.Task] = {}

    logger.info("Orchestration loop started, waiting for data layer...")

    # Wait for data layer to be ready
    while not stop_event.is_set():
        if monitor.is_data_layer_healthy():
            break
        await asyncio.sleep(2)

    logger.info("Data layer healthy, entering main loop")

    while not stop_event.is_set():
        try:
            # Check for new market (new round)
            market = _load_live_market(data_dir)
            if market:
                updated_at = market.get("updated_at", 0)

                # Detect rounds and score/invoke
                rounds_dir = os.path.join(data_dir, "rounds")
                if os.path.isdir(rounds_dir):
                    now = time.time()
                    all_rounds = sorted(
                        [
                            int(d)
                            for d in os.listdir(rounds_dir)
                            if d.isdigit() and int(d) <= now + ROUND_FUTURE_TOLERANCE_SECONDS
                        ]
                    )

                    # Score ALL rounds that are old enough and haven't been scored
                    scored_count = 0
                    for round_ts in all_rounds:
                        age = now - round_ts
                        result_path = os.path.join(data_dir, "rounds", str(round_ts), "result.json")
                        if os.path.exists(result_path):
                            scored_count += 1
                        elif age >= 300:
                            await _score_round(round_ts, data_dir, runner, fast_fail, agents_dir)
                            scored_count += 1
                            rounds_since_cleanup += 1

                    # Invoke agents for the current round from the data layer pointer.
                    current_round = _get_current_round_timestamp(data_dir)
                    if not (
                        _round_matches_accepting_market(data_dir, current_round)
                        or _round_is_tradeable(current_round, config.prediction_lock_seconds, now)
                    ):
                        current_round = None
                    latest_round = current_round if current_round is not None else (all_rounds[-1] if all_rounds else None)
                    if latest_round is not None and not (
                        _round_matches_accepting_market(data_dir, latest_round)
                        or _round_is_tradeable(latest_round, config.prediction_lock_seconds, now)
                    ):
                        latest_round = None
                    if latest_round is not None:
                        if latest_round != last_round:
                            last_round = latest_round
                        for round_ts, task in list(active_round_tasks.items()):
                            if task.done():
                                active_round_tasks.pop(round_ts, None)

                        if latest_round not in active_round_tasks:
                            if not monitor.is_data_layer_healthy():
                                logger.warning(f"Data layer unhealthy, skipping round {latest_round}")
                            else:
                                active_round_tasks[latest_round] = asyncio.create_task(
                                    _monitor_round_until_lock(
                                        latest_round, data_dir, agents_dir,
                                        runner, predictor, fast_fail, config,
                                        monitor, stop_event,
                                    )
                                )

            # Run strategy evolution every K scored rounds (non-blocking background task)
            if scored_count > 0 and scored_count - last_evolution_round_count >= config.evaluation_window_rounds:
                if evolution_task is None or evolution_task.done():
                    logger.info(f"=== EVOLUTION WINDOW ({scored_count} scored rounds, every {config.evaluation_window_rounds}) ===")
                    evolution_task = asyncio.create_task(
                        _evolve_agents(evolver, runner, fast_fail, agents_dir)
                    )
                    last_evolution_round_count = scored_count
                else:
                    logger.info("Evolution still running from previous window, skipping")

            # Run tournament cycle periodically (restart-safe)
            if scored_count > 0 and scored_count - last_tournament_round_count >= config.coordinator_frequency_rounds:
                logger.info(f"=== TOURNAMENT CYCLE ({scored_count} scored rounds) ===")
                result = tournament.run_cycle()
                actions = result.get("actions", [])
                for a in actions:
                    logger.info(f"  Tournament: {a['type']} — {json.dumps(a)}")
                last_tournament_round_count = scored_count

            # Run cleanup periodically (every ~50 rounds ≈ 4 hours)
            if rounds_since_cleanup >= 50:
                retention.run_cleanup()
                rounds_since_cleanup = 0

        except Exception as e:
            logger.error(f"Orchestration error: {e}", exc_info=True)

        # Poll every 5 seconds
        await asyncio.sleep(5)


async def run_system(project_dir: str):
    config_path = os.path.join(project_dir, "config.json")
    config = load_config(config_path) if os.path.exists(config_path) else Config()
    data_dir = os.path.join(project_dir, config.data_dir)
    agents_dir = os.path.join(project_dir, config.agents_dir)

    collector = Collector(data_dir)
    spawner = AgentSpawner(agents_dir)
    runner = AgentRunner(
        agents_dir,
        data_dir,
        config.prediction_deadline_seconds,
        config.evaluation_window_rounds,
    )
    monitor = HealthMonitor(data_dir)
    tournament = Tournament(config, spawner, runner, data_dir)
    predictor = Predictor(timeout_seconds=config.prediction_deadline_seconds)
    fast_fail = FastFailChecker(streak_threshold=config.fast_fail_streak)
    evolver = StrategyEvolver(
        agents_dir,
        data_dir,
        timeout_seconds=config.evolution_timeout_seconds,
        evaluation_window=config.evaluation_window_rounds,
    )
    retention = RetentionManager(data_dir)

    if not runner.discover_agents():
        tournament.spawn_initial_agents()
        logger.info("Initial agents spawned")

    stop_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Start data collector and orchestration loop concurrently
    collector_task = asyncio.create_task(collector.run(config))
    orchestration_task = asyncio.create_task(
        _orchestration_loop(
            data_dir, agents_dir, config, runner, predictor,
            fast_fail, evolver, tournament, retention, monitor, stop_event,
        )
    )

    logger.info("System started. Press Ctrl+C to stop.")
    await stop_event.wait()

    # Graceful shutdown
    collector.stop()
    collector_task.cancel()
    orchestration_task.cancel()
    for task in [collector_task, orchestration_task]:
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("System stopped")

def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m Strategy Discovery")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init", help="Initialize project directory")
    init_parser.add_argument("--dir", default=".", help="Project directory")
    run_parser = subparsers.add_parser("run", help="Run the system")
    run_parser.add_argument("--dir", default=".", help="Project directory")
    args = parser.parse_args()

    if args.command == "init":
        init_project(args.dir)
        print(f"Project initialized at {args.dir}")
    elif args.command == "run":
        asyncio.run(run_system(args.dir))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
