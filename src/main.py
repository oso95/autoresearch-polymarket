# src/main.py
import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from src.config import Config, load_config
from src.data_layer.collector import Collector
from src.coordinator.tournament import Tournament
from src.coordinator.spawner import AgentSpawner
from src.runner.agent_runner import AgentRunner
from src.runner.health_monitor import HealthMonitor

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

    if not runner.discover_agents():
        tournament.spawn_initial_agents()
        logger.info("Initial agents spawned")

    collector_task = asyncio.create_task(collector.run(config))
    logger.info("System started. Press Ctrl+C to stop.")

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()
    collector.stop()
    collector_task.cancel()
    try:
        await collector_task
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
