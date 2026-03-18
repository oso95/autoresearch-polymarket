# src/main.py
import argparse
import asyncio
import json
import logging
import os
import signal
import time

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
from src.io_utils import read_jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def init_project(project_dir: str):
    os.makedirs(project_dir, exist_ok=True)
    data_dir = os.path.join(project_dir, "data")
    agents_dir = os.path.join(project_dir, "agents")
    for subdir in ["live", "polling", "history", "rounds", "coordinator", "archive", "shared_knowledge"]:
        os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
    os.makedirs(agents_dir, exist_ok=True)

    # Seed shared knowledge directory with initial guidance
    shared_dir = os.path.join(data_dir, "shared_knowledge")
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

    notes_path = os.path.join(agent_dir, "notes.md")
    notes = ""
    if os.path.exists(notes_path):
        with open(notes_path) as f:
            notes = f.read()

    # Add shared knowledge
    if os.path.isdir(shared_knowledge_dir):
        shared_files = []
        for fname in sorted(os.listdir(shared_knowledge_dir)):
            fpath = os.path.join(shared_knowledge_dir, fname)
            if os.path.isfile(fpath) and fname.endswith((".md", ".txt", ".json")):
                with open(fpath) as f:
                    shared_files.append(f"### Shared: {fname}\n{f.read()}")
        if shared_files:
            notes += "\n\n## Shared Knowledge Base\n" + "\n\n".join(shared_files)

    # Recent results summary
    preds = read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
    scored = [p for p in preds if p.get("correct") is not None]
    recent = scored[-10:]
    recent_str = ", ".join("W" if p["correct"] else "L" for p in recent) if recent else "no history"

    result = await predictor.get_prediction(
        agent_dir=agent_dir,
        strategy=strategy,
        snapshot=snapshot,
        scripts=scripts,
        recent_results=recent_str,
        notes=notes,
    )

    if result is None:
        logger.warning(f"  {agent_name}: timeout/error, skipping")
        return

    strategy_version = str(hash(strategy))[:8]
    runner.record_prediction(
        agent_name, round_timestamp,
        result["prediction"], result["confidence"],
        result["reasoning"], strategy_version,
    )
    logger.info(f"  {agent_name}: {result['prediction']} (confidence {result['confidence']:.0%}) — {result['reasoning'][:80]}")


async def _run_round(
    round_timestamp: int,
    data_dir: str,
    agents_dir: str,
    runner: AgentRunner,
    predictor: Predictor,
    fast_fail: FastFailChecker,
    config: Config,
):
    """Invoke ALL agents in PARALLEL for a single round."""
    snapshot_path = os.path.join(data_dir, "rounds", str(round_timestamp), "snapshot.json")
    if not os.path.exists(snapshot_path):
        logger.warning(f"No snapshot for round {round_timestamp}, skipping")
        return

    with open(snapshot_path) as f:
        snapshot = json.load(f)

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

    agents = runner.discover_agents()
    shared_knowledge_dir = os.path.join(data_dir, "shared_knowledge")
    logger.info(f"Round {round_timestamp}: invoking {len(agents)} agents (max 3 concurrent)")

    # Use semaphore to limit concurrent Claude CLI calls (avoid rate limits)
    sem = asyncio.Semaphore(3)

    async def _limited_invoke(agent_name):
        async with sem:
            return await _invoke_single_agent(
                agent_name, agents_dir, snapshot, predictor,
                runner, round_timestamp, shared_knowledge_dir,
            )

    tasks = [_limited_invoke(name) for name in agents]
    await asyncio.gather(*tasks, return_exceptions=True)


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
        # Only score if agent made a prediction for this round
        preds = read_jsonl(os.path.join(agents_dir, agent_name, "predictions.jsonl"))
        has_prediction = any(p["round"] == round_timestamp and p.get("outcome") is None for p in preds)
        if not has_prediction:
            continue

        runner.score_round(agent_name, round_timestamp, outcome)
        scored_count += 1

        # Check if prediction was correct
        preds_after = read_jsonl(os.path.join(agents_dir, agent_name, "predictions.jsonl"))
        for p in preds_after:
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


async def _evolve_agents(evolver: StrategyEvolver, runner: AgentRunner, fast_fail: FastFailChecker, agents_dir: str):
    """Run the autoresearch inner loop: evolve each agent's strategy."""
    agents = runner.discover_agents()
    for agent_name in agents:
        # Only evolve agents that have enough data
        preds = read_jsonl(os.path.join(agents_dir, agent_name, "predictions.jsonl"))
        scored = [p for p in preds if p.get("correct") is not None]
        if len(scored) < 3:
            continue

        logger.info(f"  Evolving {agent_name}...")
        result = await evolver.evolve_agent(agent_name)
        if result:
            evolver.apply_evolution(agent_name, result)
            change = result.get("change_description", "unknown")
            logger.info(f"  {agent_name} evolved: {change}")
        else:
            logger.warning(f"  {agent_name} evolution failed (will retry next window)")


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
            market_path = os.path.join(data_dir, "live", "polymarket_market.json")
            if os.path.exists(market_path):
                with open(market_path) as f:
                    market = json.load(f)
                updated_at = market.get("updated_at", 0)

                # Detect rounds and score/invoke
                rounds_dir = os.path.join(data_dir, "rounds")
                if os.path.isdir(rounds_dir):
                    all_rounds = sorted(
                        [int(d) for d in os.listdir(rounds_dir) if d.isdigit()]
                    )

                    # Score ALL rounds that are old enough and haven't been scored
                    now = time.time()
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

                    # Invoke agents for the latest round (if new)
                    if all_rounds:
                        latest_round = all_rounds[-1]
                        if latest_round != last_round:
                            last_round = latest_round
                            if not monitor.is_data_layer_healthy():
                                logger.warning(f"Data layer unhealthy, skipping round {latest_round}")
                            else:
                                await _run_round(
                                    latest_round, data_dir, agents_dir,
                                    runner, predictor, fast_fail, config,
                                )

            # Run strategy evolution every K scored rounds (restart-safe)
            if scored_count > 0 and scored_count - last_evolution_round_count >= config.evaluation_window_rounds:
                logger.info(f"=== EVOLUTION WINDOW ({scored_count} scored rounds, every {config.evaluation_window_rounds}) ===")
                await _evolve_agents(evolver, runner, fast_fail, agents_dir)
                last_evolution_round_count = scored_count

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
    runner = AgentRunner(agents_dir, data_dir, config.prediction_deadline_seconds)
    monitor = HealthMonitor(data_dir)
    tournament = Tournament(config, spawner, runner, data_dir)
    predictor = Predictor(timeout_seconds=config.prediction_deadline_seconds)
    fast_fail = FastFailChecker(streak_threshold=config.fast_fail_streak)
    evolver = StrategyEvolver(agents_dir, data_dir, timeout_seconds=120)
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
