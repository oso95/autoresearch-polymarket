#!/usr/bin/env python3
"""
Local autoresearch runner for the Polymarket strategy discovery system.

This provides a local CLI analogue to `/autoresearch:polymarket` that can be
run directly from a normal terminal without app-specific slash commands.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from src.codex_cli import DEFAULT_PREDICTION_MODEL
from src.config import load_config
from src.main import init_project, run_system
from src.runner.agent_runner import AgentRunner
from src.runner.backtester import Backtester, load_historical_rounds, split_train_test
from src.runner.evolver import StrategyEvolver
from strategy_factory import run_factory_cycle
from fast_evolve import fast_evolve_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUTORESEARCH-LOCAL] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _cmd_run_live(project_dir: str):
    init_project(project_dir)
    await run_system(project_dir)


async def _cmd_optimize(project_dir: str, cycles: int):
    init_project(project_dir)
    cwd = os.getcwd()
    try:
        os.chdir(os.path.dirname(__file__))
        for cycle in range(1, cycles + 1):
            await run_factory_cycle(cycle)
    finally:
        os.chdir(cwd)


async def _cmd_fast_evolve(
    project_dir: str,
    iterations: int,
    split: float,
    model: str,
    concurrency: int,
    agent_concurrency: int,
    agents: list[str] | None,
):
    data_dir = os.path.join(project_dir, "data")
    agents_dir = os.path.join(project_dir, "agents")
    all_rounds = load_historical_rounds(data_dir)
    if not all_rounds:
        raise SystemExit("No historical rounds found. Run live collection first.")

    train_rounds, test_rounds = split_train_test(all_rounds, split)
    runner = AgentRunner(agents_dir, data_dir)
    agent_names = agents or runner.discover_agents()
    if not agent_names:
        raise SystemExit("No agents found to evolve.")

    backtester = Backtester(
        agents_dir=agents_dir,
        data_dir=data_dir,
        model=model,
        concurrency=concurrency,
    )
    config_path = os.path.join(project_dir, "config.json")
    config = load_config(config_path)
    evolver = StrategyEvolver(
        agents_dir=agents_dir,
        data_dir=data_dir,
        timeout_seconds=config.evolution_timeout_seconds,
        evaluation_window=config.evaluation_window_rounds,
    )

    sem = asyncio.Semaphore(agent_concurrency)

    async def _one(agent_name: str):
        async with sem:
            return await fast_evolve_agent(
                agent_name=agent_name,
                agents_dir=agents_dir,
                data_dir=data_dir,
                train_rounds=train_rounds,
                test_rounds=test_rounds,
                backtester=backtester,
                evolver=evolver,
                iterations=iterations,
            )

    results = await asyncio.gather(*[_one(name) for name in agent_names], return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("Fast evolve worker failed: %s", result)
            continue
        logger.info(
            "Fast evolve complete for %s: final test WR %.1f%%",
            result["agent"],
            result.get("final_test_wr", 0.0) * 100,
        )


async def _cmd_backtest(project_dir: str, model: str, agent_concurrency: int, agents: list[str] | None):
    data_dir = os.path.join(project_dir, "data")
    agents_dir = os.path.join(project_dir, "agents")
    rounds = load_historical_rounds(data_dir)
    if not rounds:
        raise SystemExit("No historical rounds found. Run live collection first.")

    runner = AgentRunner(agents_dir, data_dir)
    agent_names = agents or runner.discover_agents()
    if not agent_names:
        raise SystemExit("No agents found to backtest.")

    bt = Backtester(
        agents_dir=agents_dir,
        data_dir=data_dir,
        model=model,
    )
    results = await bt.backtest_all(agent_names, rounds, agent_concurrency=agent_concurrency)
    bt.print_results(results, label=f"ALL ({len(rounds)} rounds)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local autoresearch runner for polymarket strategy discovery",
    )
    parser.add_argument(
        "--dir",
        default="./live-run",
        help="Project directory containing data/ and agents/ (default: ./live-run)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("live", help="Run the live data+prediction system")

    optimize = subparsers.add_parser("optimize", help="Run strategy factory optimization cycles")
    optimize.add_argument("--cycles", type=int, default=1, help="Number of optimization cycles to run")

    fast = subparsers.add_parser("fast-evolve", help="Run bounded fast-evolution with backtesting")
    fast.add_argument("--iterations", type=int, default=2, help="Evolution iterations per agent")
    fast.add_argument("--split", type=float, default=0.7, help="Train/test split ratio")
    fast.add_argument("--model", default=DEFAULT_PREDICTION_MODEL, help="Prediction model for backtesting")
    fast.add_argument("--concurrency", type=int, default=8, help="Prediction concurrency")
    fast.add_argument("--agent-concurrency", type=int, default=2, help="Parallel agent workers")
    fast.add_argument("--agents", nargs="*", help="Specific agents to evolve")

    backtest = subparsers.add_parser("backtest", help="Run a full backtest")
    backtest.add_argument("--model", default=DEFAULT_PREDICTION_MODEL, help="Prediction model")
    backtest.add_argument("--agent-concurrency", type=int, default=3, help="Parallel agent workers")
    backtest.add_argument("--agents", nargs="*", help="Specific agents to backtest")

    return parser


async def main():
    parser = _build_parser()
    args = parser.parse_args()
    project_dir = args.dir

    if args.command == "live":
        await _cmd_run_live(project_dir)
    elif args.command == "optimize":
        await _cmd_optimize(project_dir, args.cycles)
    elif args.command == "fast-evolve":
        await _cmd_fast_evolve(
            project_dir=project_dir,
            iterations=args.iterations,
            split=args.split,
            model=args.model,
            concurrency=args.concurrency,
            agent_concurrency=args.agent_concurrency,
            agents=args.agents,
        )
    elif args.command == "backtest":
        await _cmd_backtest(
            project_dir=project_dir,
            model=args.model,
            agent_concurrency=args.agent_concurrency,
            agents=args.agents,
        )
    else:
        parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    asyncio.run(main())
