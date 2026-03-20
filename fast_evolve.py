#!/usr/bin/env python3
"""
Fast Evolution Loop — accelerate strategy discovery using backtesting.

Instead of waiting 5 minutes per round in live trading, this loop:
1. Backtests agent on TRAIN set → baseline win rate
2. Evolves strategy via Codex/GPT
3. Backtests evolved strategy on TRAIN set → new win rate
4. Validates on TEST set → check for overfitting
5. Keep if better + not overfit, revert if worse

One iteration = what would take hours in live trading, done in minutes.

Usage:
  # Evolve all agents for 3 iterations
  python3 fast_evolve.py --iterations 3

  # Evolve specific agents
  python3 fast_evolve.py --agents agent-044-yi-jing-oracle agent-047-tarot-arcana --iterations 5

  # Use different train/test split
  python3 fast_evolve.py --split 0.6 --iterations 3

  # Dry run (backtest only, no evolution)
  python3 fast_evolve.py --dry-run
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from src.codex_cli import DEFAULT_PREDICTION_MODEL
from src.runner.backtester import Backtester, load_historical_rounds, split_train_test
from src.runner.evolver import StrategyEvolver
from src.runner.agent_runner import AgentRunner
from src.io_utils import read_jsonl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Maximum acceptable gap between train and test win rates
# If train WR - test WR > this, the strategy is likely overfit
OVERFIT_THRESHOLD = 0.15


async def fast_evolve_agent(
    agent_name: str,
    agents_dir: str,
    data_dir: str,
    train_rounds: list[dict],
    test_rounds: list[dict],
    backtester: Backtester,
    evolver: StrategyEvolver,
    iterations: int = 3,
) -> dict:
    """Run fast evolution loop for a single agent."""
    agent_dir = os.path.join(agents_dir, agent_name)
    history = []

    # Skip mirror agents
    config_path = os.path.join(agent_dir, "agent_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            if json.load(f).get("mirror"):
                logger.info(f"  {agent_name}: mirror agent, skipping evolution")
                return {"agent": agent_name, "skipped": True, "reason": "mirror"}

    for iteration in range(iterations):
        iter_start = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"  {agent_name} — Iteration {iteration + 1}/{iterations}")
        logger.info(f"{'='*60}")

        # Step 1: Backtest current strategy on TRAIN set
        logger.info(f"  Step 1: Backtesting on train set ({len(train_rounds)} rounds)...")
        train_result = await backtester.backtest_agent(agent_name, train_rounds)
        if "error" in train_result:
            logger.error(f"  Backtest error: {train_result['error']}")
            break

        train_wr = train_result["win_rate"]
        logger.info(f"  Train WR: {train_wr:.1%} ({train_result['wins']}/{train_result['total']})")

        # Step 2: Validate on TEST set
        logger.info(f"  Step 2: Validating on test set ({len(test_rounds)} rounds)...")
        test_result = await backtester.backtest_agent(agent_name, test_rounds)
        test_wr = test_result["win_rate"] if "error" not in test_result else 0.0
        logger.info(f"  Test WR: {test_wr:.1%}")

        baseline = {
            "iteration": iteration,
            "phase": "baseline",
            "train_wr": train_wr,
            "test_wr": test_wr,
            "gap": train_wr - test_wr,
        }
        history.append(baseline)

        # Step 3: Evolve strategy
        logger.info(f"  Step 3: Evolving strategy via Codex/GPT...")
        evo_result = await evolver.evolve_agent(agent_name)
        if not evo_result:
            logger.warning(f"  Evolution failed, keeping current strategy")
            continue

        # Apply evolution (backs up old strategy automatically)
        evolver.apply_evolution(agent_name, evo_result)
        change = evo_result.get("change_description", "unknown")
        logger.info(f"  Evolved: {change}")

        # Step 4: Backtest evolved strategy on TRAIN set
        logger.info(f"  Step 4: Backtesting evolved strategy on train set...")
        new_train_result = await backtester.backtest_agent(agent_name, train_rounds)
        new_train_wr = new_train_result["win_rate"] if "error" not in new_train_result else 0.0

        # Step 5: Validate evolved strategy on TEST set
        logger.info(f"  Step 5: Validating evolved strategy on test set...")
        new_test_result = await backtester.backtest_agent(agent_name, test_rounds)
        new_test_wr = new_test_result["win_rate"] if "error" not in new_test_result else 0.0

        gap = new_train_wr - new_test_wr
        improved = new_test_wr > test_wr  # Compare TEST performance (not train!)
        overfit = gap > OVERFIT_THRESHOLD

        evolved = {
            "iteration": iteration,
            "phase": "evolved",
            "train_wr": new_train_wr,
            "test_wr": new_test_wr,
            "gap": gap,
            "change": change,
            "improved": improved,
            "overfit": overfit,
        }
        history.append(evolved)

        elapsed = time.time() - iter_start

        # Decision: keep or revert
        if improved and not overfit:
            logger.info(f"  KEEP: Test WR {test_wr:.1%} → {new_test_wr:.1%} (+{new_test_wr - test_wr:.1%}), gap {gap:.1%} [{elapsed:.0f}s]")
        elif overfit:
            logger.warning(f"  REVERT (overfit): Train {new_train_wr:.1%} vs Test {new_test_wr:.1%} (gap {gap:.1%}) [{elapsed:.0f}s]")
            _revert_strategy(agent_dir)
        else:
            logger.warning(f"  REVERT (no improvement): Test WR {test_wr:.1%} → {new_test_wr:.1%} [{elapsed:.0f}s]")
            _revert_strategy(agent_dir)

    return {
        "agent": agent_name,
        "history": history,
        "final_train_wr": history[-1]["train_wr"] if history else 0,
        "final_test_wr": history[-1]["test_wr"] if history else 0,
    }


def _revert_strategy(agent_dir: str):
    """Revert to the previous strategy (backed up by evolver)."""
    prev_path = os.path.join(agent_dir, "strategy.md.prev")
    strategy_path = os.path.join(agent_dir, "strategy.md")
    if os.path.exists(prev_path):
        shutil.copy2(prev_path, strategy_path)
        logger.info(f"  Reverted to previous strategy")
    else:
        logger.warning(f"  No previous strategy to revert to")


async def main():
    parser = argparse.ArgumentParser(description="Fast evolution loop using backtesting")
    parser.add_argument("--dir", default="./live-run", help="Project directory")
    parser.add_argument("--agents", nargs="*", help="Specific agents to evolve (default: all)")
    parser.add_argument("--iterations", type=int, default=3, help="Evolution iterations per agent")
    parser.add_argument("--split", type=float, default=0.7, help="Train/test split ratio")
    parser.add_argument("--concurrency", type=int, default=8, help="Prediction concurrency")
    parser.add_argument("--agent-concurrency", type=int, default=2,
                        help="Agents evolving in parallel (default: 2)")
    parser.add_argument("--model", default=DEFAULT_PREDICTION_MODEL, help=f"Prediction model (default: {DEFAULT_PREDICTION_MODEL})")
    parser.add_argument("--evolution-timeout", type=int, default=900,
                        help="Seconds to allow each evolution call before timing out")
    parser.add_argument("--dry-run", action="store_true", help="Only backtest, no evolution")
    args = parser.parse_args()

    data_dir = os.path.join(args.dir, "data")
    agents_dir = os.path.join(args.dir, "agents")

    # Load historical rounds
    all_rounds = load_historical_rounds(data_dir)
    if not all_rounds:
        print("ERROR: No historical rounds found.")
        sys.exit(1)

    train_rounds, test_rounds = split_train_test(all_rounds, args.split)
    print(f"Loaded {len(all_rounds)} rounds: {len(train_rounds)} train, {len(test_rounds)} test")

    # Discover agents
    runner = AgentRunner(agents_dir, data_dir)
    all_agents = runner.discover_agents()

    if args.agents:
        agent_names = [a for a in args.agents if a in all_agents]
    else:
        agent_names = all_agents

    print(f"Evolving {len(agent_names)} agents, {args.iterations} iterations each")

    bt = Backtester(
        agents_dir=agents_dir,
        data_dir=data_dir,
        model=args.model,
        concurrency=args.concurrency,
    )

    if args.dry_run:
        print("\n--- DRY RUN: Backtest only (no evolution) ---")
        results = await bt.backtest_all(agent_names, all_rounds, args.agent_concurrency)
        bt.print_results(results, label="Full backtest (dry run)")
        return

    evolver = StrategyEvolver(agents_dir, data_dir, timeout_seconds=args.evolution_timeout)

    # Run fast evolution for each agent
    sem = asyncio.Semaphore(args.agent_concurrency)
    all_results = []

    async def _evolve_one(name):
        async with sem:
            return await fast_evolve_agent(
                name, agents_dir, data_dir,
                train_rounds, test_rounds,
                bt, evolver, args.iterations,
            )

    tasks = [_evolve_one(name) for name in agent_names]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Summary
    print(f"\n{'='*80}")
    print(f"  FAST EVOLUTION COMPLETE")
    print(f"{'='*80}")
    print(f"{'Agent':<50} {'Final Test WR':>13} {'Status':>10}")
    print(f"{'-'*50} {'-'*13} {'-'*10}")

    for r in all_results:
        if isinstance(r, Exception):
            print(f"  ERROR: {r}")
            continue
        if r.get("skipped"):
            print(f"{r['agent']:<50} {'—':>13} {'skipped':>10}")
            continue
        kept = sum(1 for h in r.get("history", []) if h.get("phase") == "evolved" and h.get("improved") and not h.get("overfit"))
        reverted = sum(1 for h in r.get("history", []) if h.get("phase") == "evolved" and not (h.get("improved") and not h.get("overfit")))
        print(f"{r['agent']:<50} {r.get('final_test_wr', 0):>12.1%} {kept}K/{reverted}R")

    # Save results
    output_path = os.path.join(data_dir, "fast-evolution-results.json")
    with open(output_path, "w") as f:
        json.dump({
            "timestamp": int(time.time()),
            "iterations": args.iterations,
            "train_rounds": len(train_rounds),
            "test_rounds": len(test_rounds),
            "results": [r for r in all_results if not isinstance(r, Exception)],
        }, f, indent=2)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
