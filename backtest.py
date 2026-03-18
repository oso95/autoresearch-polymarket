#!/usr/bin/env python3
"""
Run backtesting on historical data for rapid strategy evaluation.

Usage:
  # Backtest all agents on all historical rounds
  python3 backtest.py

  # Backtest specific agents
  python3 backtest.py --agents agent-040-mirror-orderbook-specialist agent-001-orderbook-specialist

  # Train/test split to check for overfitting
  python3 backtest.py --split 0.7

  # Use a specific model for all agents (override per-agent config)
  python3 backtest.py --model sonnet

  # Higher concurrency for faster runs
  python3 backtest.py --concurrency 15 --agent-concurrency 5
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from src.runner.backtester import Backtester, load_historical_rounds, split_train_test
from src.runner.agent_runner import AgentRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="Backtest agent strategies on historical data")
    parser.add_argument("--dir", default="./live-run", help="Project directory")
    parser.add_argument("--agents", nargs="*", help="Specific agents to test (default: all)")
    parser.add_argument("--split", type=float, default=0.0,
                        help="Train/test split ratio (e.g., 0.7 = 70%% train, 30%% test). 0 = no split.")
    parser.add_argument("--model", default=None,
                        help="Override model for all agents (haiku, sonnet, opus)")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Max concurrent predictions per agent (default: 10)")
    parser.add_argument("--agent-concurrency", type=int, default=3,
                        help="Max agents backtesting in parallel (default: 3)")
    parser.add_argument("--timeout", type=int, default=90,
                        help="Prediction timeout in seconds (default: 90)")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Rounds per Claude call in batch mode (default: 10, use 1 for single mode)")
    parser.add_argument("--output", default=None,
                        help="Output JSON file path (default: live-run/data/backtest-results.json)")
    args = parser.parse_args()

    data_dir = os.path.join(args.dir, "data")
    agents_dir = os.path.join(args.dir, "agents")

    # Load historical rounds
    rounds = load_historical_rounds(data_dir)
    if not rounds:
        print("ERROR: No historical rounds found. Run the system first to collect data.")
        sys.exit(1)

    print(f"Loaded {len(rounds)} historical rounds")
    print(f"  Period: {rounds[0]['timestamp']} → {rounds[-1]['timestamp']}")

    # Discover agents
    runner = AgentRunner(agents_dir, data_dir)
    all_agents = runner.discover_agents()

    if args.agents:
        agent_names = [a for a in args.agents if a in all_agents]
        missing = set(args.agents) - set(all_agents)
        if missing:
            print(f"WARNING: Agents not found: {missing}")
    else:
        agent_names = all_agents

    print(f"Testing {len(agent_names)} agents")

    # Initialize backtester
    bt = Backtester(
        agents_dir=agents_dir,
        data_dir=data_dir,
        model=args.model or "haiku",
        timeout=args.timeout,
        concurrency=args.concurrency,
        batch_size=args.batch_size,
    )

    if args.split > 0:
        # Train/test split
        train_rounds, test_rounds = split_train_test(rounds, args.split)
        print(f"\nTrain/test split: {len(train_rounds)} train, {len(test_rounds)} test")

        print(f"\n--- TRAIN SET ({len(train_rounds)} rounds) ---")
        train_results = await bt.backtest_all(agent_names, train_rounds, args.agent_concurrency)
        bt.print_results(train_results, label="TRAIN")

        print(f"\n--- TEST SET ({len(test_rounds)} rounds) ---")
        test_results = await bt.backtest_all(agent_names, test_rounds, args.agent_concurrency)
        bt.print_results(test_results, label="TEST")

        # Overfitting analysis
        print(f"\n{'=' * 80}")
        print(f"  OVERFITTING ANALYSIS (train - test gap)")
        print(f"{'=' * 80}")
        train_map = {r["agent"]: r for r in train_results if "error" not in r}
        test_map = {r["agent"]: r for r in test_results if "error" not in r}
        gaps = []
        for name in agent_names:
            if name in train_map and name in test_map:
                gap = train_map[name]["win_rate"] - test_map[name]["win_rate"]
                gaps.append((name, train_map[name]["win_rate"], test_map[name]["win_rate"], gap))

        gaps.sort(key=lambda x: abs(x[3]), reverse=True)
        print(f"{'Agent':<50} {'Train':>6} {'Test':>6} {'Gap':>7}")
        print(f"{'-' * 50} {'-' * 6} {'-' * 6} {'-' * 7}")
        for name, train_wr, test_wr, gap in gaps:
            flag = " *** OVERFIT" if gap > 0.15 else ""
            print(f"{name:<50} {train_wr:>5.1%} {test_wr:>5.1%} {gap:>+6.1%}{flag}")

        output = args.output or os.path.join(data_dir, "backtest-results-split.json")
        bt.save_results(train_results + test_results, output, "train-test-split")
    else:
        # Full backtest (no split)
        results = await bt.backtest_all(agent_names, rounds, args.agent_concurrency)
        bt.print_results(results, label=f"ALL ({len(rounds)} rounds)")

        output = args.output or os.path.join(data_dir, "backtest-results.json")
        bt.save_results(results, output, "full-backtest")

    print(f"\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
