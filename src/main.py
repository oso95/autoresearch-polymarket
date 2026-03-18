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
from src.io_utils import read_jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def init_project(project_dir: str):
    os.makedirs(project_dir, exist_ok=True)
    data_dir = os.path.join(project_dir, "data")
    agents_dir = os.path.join(project_dir, "agents")
    for subdir in ["live", "polling", "history", "rounds", "coordinator", "archive"]:
        os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
    os.makedirs(agents_dir, exist_ok=True)
    config_path = os.path.join(project_dir, "config.json")
    if not os.path.exists(config_path):
        config = Config()
        with open(config_path, "w") as f:
            json.dump({k: v for k, v in config.__dict__.items()}, f, indent=2)


async def _run_round(
    round_timestamp: int,
    data_dir: str,
    agents_dir: str,
    runner: AgentRunner,
    predictor: Predictor,
    fast_fail: FastFailChecker,
    config: Config,
):
    """Invoke all agents for a single round, collect predictions."""
    snapshot_path = os.path.join(data_dir, "rounds", str(round_timestamp), "snapshot.json")
    if not os.path.exists(snapshot_path):
        logger.warning(f"No snapshot for round {round_timestamp}, skipping")
        return

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    agents = runner.discover_agents()
    logger.info(f"Round {round_timestamp}: invoking {len(agents)} agents")

    for agent_name in agents:
        agent_dir = os.path.join(agents_dir, agent_name)

        # Read agent's strategy and scripts
        strategy_path = os.path.join(agent_dir, "strategy.md")
        if not os.path.exists(strategy_path):
            continue
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

        # Recent results summary
        preds = read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
        scored = [p for p in preds if p.get("correct") is not None]
        recent = scored[-10:]
        recent_str = ", ".join("W" if p["correct"] else "L" for p in recent) if recent else "no history"

        # Get prediction from Claude
        result = await predictor.get_prediction(
            agent_dir=agent_dir,
            strategy=strategy,
            snapshot=snapshot,
            scripts=scripts,
            recent_results=recent_str,
            notes=notes,
        )

        if result is None:
            logger.warning(f"Agent {agent_name} returned no prediction (timeout/error), skipping")
            continue

        # Record the prediction
        # Use git HEAD as strategy version
        strategy_version = str(hash(strategy))[:8]
        runner.record_prediction(
            agent_name, round_timestamp,
            result["prediction"], result["confidence"],
            result["reasoning"], strategy_version,
        )
        logger.info(f"  {agent_name}: {result['prediction']} (confidence {result['confidence']:.0%})")


async def _score_round(round_timestamp: int, data_dir: str, runner: AgentRunner, fast_fail: FastFailChecker, agents_dir: str):
    """Score all agents for a resolved round."""
    result_path = os.path.join(data_dir, "rounds", str(round_timestamp), "result.json")
    if not os.path.exists(result_path):
        return

    with open(result_path) as f:
        result = json.load(f)

    outcome = result["outcome"]
    agents = runner.discover_agents()

    for agent_name in agents:
        runner.score_round(agent_name, round_timestamp, outcome)

        # Check fast-fail
        agent_dir = os.path.join(agents_dir, agent_name)
        if fast_fail.should_revert(agent_dir):
            fast_fail.revert_strategy(agent_dir)
            logger.info(f"  {agent_name}: FAST-FAIL triggered, reverted strategy")

    logger.info(f"Round {round_timestamp} scored: outcome={outcome}")


async def _orchestration_loop(
    data_dir: str,
    agents_dir: str,
    config: Config,
    runner: AgentRunner,
    predictor: Predictor,
    fast_fail: FastFailChecker,
    tournament: Tournament,
    retention: RetentionManager,
    monitor: HealthMonitor,
    stop_event: asyncio.Event,
):
    """Main orchestration: detect rounds, invoke agents, score, run tournament."""
    last_round: int | None = None
    rounds_since_tournament = 0
    rounds_since_cleanup = 0
    pending_score: int | None = None

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

                # Detect new round by checking for new round directories
                rounds_dir = os.path.join(data_dir, "rounds")
                if os.path.isdir(rounds_dir):
                    round_dirs = sorted(
                        [d for d in os.listdir(rounds_dir) if d.isdigit()],
                        key=int, reverse=True
                    )
                    if round_dirs:
                        latest_round = int(round_dirs[0])

                        # Score previous round if it resolved
                        if pending_score is not None and pending_score != latest_round:
                            result_path = os.path.join(data_dir, "rounds", str(pending_score), "result.json")
                            if os.path.exists(result_path):
                                await _score_round(pending_score, data_dir, runner, fast_fail, agents_dir)
                                rounds_since_tournament += 1
                                rounds_since_cleanup += 1
                                pending_score = None

                        # New round detected
                        if latest_round != last_round:
                            last_round = latest_round

                            # Check data layer health
                            if monitor.is_round_data_stale():
                                logger.warning(f"Data stale, skipping round {latest_round}")
                            else:
                                await _run_round(
                                    latest_round, data_dir, agents_dir,
                                    runner, predictor, fast_fail, config,
                                )
                                pending_score = latest_round

            # Run tournament cycle periodically
            if rounds_since_tournament >= config.coordinator_frequency_rounds:
                logger.info("Running tournament cycle...")
                result = tournament.run_cycle()
                actions = result.get("actions", [])
                for a in actions:
                    logger.info(f"  Tournament: {a['type']} — {json.dumps(a)}")
                rounds_since_tournament = 0

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
            fast_fail, tournament, retention, monitor, stop_event,
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
