#!/usr/bin/env python3
"""
Strategy Factory — Continuous optimization loop that runs alongside live trading.

This is the "factory" that never stops improving:
1. ANALYZE: Read shared knowledge + leaderboard for insights
2. BACKTEST: Test all agents on historical data
3. IDENTIFY: Find underperformers and overperformers
4. EVOLVE: Fast-evolve the worst non-mirror agents
5. SYNTHESIZE: Generate new strategies from shared discoveries
6. PRUNE: Retire hopeless agents, mirror anti-predictive ones
7. LOOP: Wait for more data, repeat

Runs forever alongside the live trading system.
"""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from src.runner.backtester import Backtester, load_historical_rounds, split_train_test
from src.runner.evolver import StrategyEvolver
from src.runner.agent_runner import AgentRunner
from src.coordinator.spawner import AgentSpawner
from src.coordinator.tournament import Tournament
from src.config import load_config
from src.io_utils import read_jsonl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FACTORY] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROJECT_DIR = "./live-run"
DATA_DIR = os.path.join(PROJECT_DIR, "data")
AGENTS_DIR = os.path.join(PROJECT_DIR, "agents")
SHARED_DIR = os.path.join(DATA_DIR, "shared_knowledge")

# Factory settings
CYCLE_INTERVAL_SECONDS = 600       # Run optimization every 10 minutes
MIN_ROUNDS_FOR_BACKTEST = 30       # Need at least 30 historical rounds
EVOLVE_THRESHOLD_WR = 0.45         # Evolve agents below this win rate
EVOLVE_MIN_ROUNDS = 15             # Only evolve agents with enough data
EVOLVE_ITERATIONS = 2              # Fast-evolve iterations per cycle
MIRROR_THRESHOLD_WR = 0.35         # Auto-mirror below this
MIRROR_MIN_ROUNDS = 20             # Minimum rounds for mirror confidence
MAX_AGENTS = 55                    # Don't let the pool grow too large


def get_agent_stats() -> list[dict]:
    """Get current stats for all agents."""
    stats = []
    for name in sorted(os.listdir(AGENTS_DIR)):
        agent_dir = os.path.join(AGENTS_DIR, name)
        if not os.path.isdir(agent_dir) or not name.startswith("agent-"):
            continue
        preds = read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
        scored = [p for p in preds if p.get("correct") is not None]
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored) if scored else 0

        config = {}
        cp = os.path.join(agent_dir, "agent_config.json")
        if os.path.exists(cp):
            with open(cp) as f:
                config = json.load(f)

        stats.append({
            "name": name,
            "win_rate": wr,
            "rounds": len(scored),
            "wins": wins,
            "mirror": config.get("mirror", False),
            "model": config.get("model", "haiku"),
            "is_ensemble": "ensemble" in name,
        })
    return stats


def read_shared_insights() -> str:
    """Read the latest shared knowledge for strategy synthesis."""
    if not os.path.isdir(SHARED_DIR):
        return ""

    # Read core files
    insights = []
    for fname in ["approaches.md", "tournament-insights.md"]:
        fpath = os.path.join(SHARED_DIR, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                insights.append(f.read())

    # Read last 5 discoveries
    discoveries = sorted([
        f for f in os.listdir(SHARED_DIR) if f.startswith("discovery-")
    ])
    for fname in discoveries[-5:]:
        with open(os.path.join(SHARED_DIR, fname)) as f:
            insights.append(f.read())

    return "\n\n---\n\n".join(insights)


async def run_factory_cycle(cycle_num: int):
    """Run one optimization cycle."""
    logger.info(f"{'='*60}")
    logger.info(f"  FACTORY CYCLE #{cycle_num}")
    logger.info(f"{'='*60}")

    # Phase 1: ANALYZE
    logger.info("Phase 1: ANALYZE — reading current state")
    stats = get_agent_stats()
    total_agents = len(stats)
    proven = [s for s in stats if s["rounds"] >= 20]
    above_50 = [s for s in proven if s["win_rate"] > 0.50]

    logger.info(f"  {total_agents} agents, {len(proven)} with 20+ rounds, {len(above_50)} above 50%")

    if proven:
        best = max(proven, key=lambda s: s["win_rate"])
        worst = min(proven, key=lambda s: s["win_rate"])
        logger.info(f"  Best proven: {best['name']} ({best['win_rate']:.1%}, {best['rounds']}r)")
        logger.info(f"  Worst proven: {worst['name']} ({worst['win_rate']:.1%}, {worst['rounds']}r)")

    # Phase 2: BACKTEST (if enough historical data)
    rounds = load_historical_rounds(DATA_DIR)
    if len(rounds) < MIN_ROUNDS_FOR_BACKTEST:
        logger.info(f"  Only {len(rounds)} rounds — need {MIN_ROUNDS_FOR_BACKTEST} for backtest, skipping")
    else:
        logger.info(f"Phase 2: BACKTEST — {len(rounds)} historical rounds available")

        # Only backtest agents that have been evolving (not mirrors, not ensembles)
        bt_candidates = [
            s["name"] for s in stats
            if not s["mirror"]
            and not s["is_ensemble"]
            and s["rounds"] >= 5
        ]

        if bt_candidates:
            bt = Backtester(
                agents_dir=AGENTS_DIR,
                data_dir=DATA_DIR,
                model="haiku",
                timeout=90,
                concurrency=8,
                batch_size=10,
            )

            # Quick backtest on test set only (last 30%)
            _, test_rounds = split_train_test(rounds, 0.7)
            if len(test_rounds) >= 10:
                logger.info(f"  Backtesting {len(bt_candidates)} agents on {len(test_rounds)} test rounds...")
                bt_results = await bt.backtest_all(bt_candidates, test_rounds, agent_concurrency=3)

                for r in sorted(bt_results, key=lambda x: x.get("win_rate", 0), reverse=True):
                    if "error" not in r:
                        logger.info(f"    {r['agent']}: {r['win_rate']:.1%} (backtest on test set)")

    # Phase 3: IDENTIFY — find agents to evolve
    logger.info("Phase 3: IDENTIFY — finding evolution targets")
    evolve_targets = [
        s for s in stats
        if s["rounds"] >= EVOLVE_MIN_ROUNDS
        and s["win_rate"] < EVOLVE_THRESHOLD_WR
        and not s["mirror"]
        and not s["is_ensemble"]
    ]

    if evolve_targets:
        logger.info(f"  {len(evolve_targets)} agents below {EVOLVE_THRESHOLD_WR:.0%} with {EVOLVE_MIN_ROUNDS}+ rounds:")
        for s in evolve_targets:
            logger.info(f"    {s['name']}: {s['win_rate']:.1%} ({s['rounds']}r)")
    else:
        logger.info("  No agents need evolution this cycle")

    # Phase 4: EVOLVE — fast-evolve underperformers
    if evolve_targets and len(rounds) >= MIN_ROUNDS_FOR_BACKTEST:
        logger.info(f"Phase 4: EVOLVE — fast-evolving {len(evolve_targets)} agents ({EVOLVE_ITERATIONS} iterations)")
        evolver = StrategyEvolver(AGENTS_DIR, DATA_DIR, timeout_seconds=180)
        bt = Backtester(
            agents_dir=AGENTS_DIR,
            data_dir=DATA_DIR,
            model="haiku",
            timeout=90,
            concurrency=8,
            batch_size=10,
        )
        train_rounds, test_rounds = split_train_test(rounds, 0.7)

        for s in evolve_targets[:3]:  # Max 3 agents per cycle to save API calls
            agent_name = s["name"]
            logger.info(f"  Evolving {agent_name} ({s['win_rate']:.1%})...")

            for i in range(EVOLVE_ITERATIONS):
                # Backtest before
                before = await bt.backtest_agent(agent_name, test_rounds)
                before_wr = before.get("win_rate", 0) if "error" not in before else 0

                # Evolve
                result = await evolver.evolve_agent(agent_name)
                if not result:
                    logger.warning(f"    Evolution failed for {agent_name}")
                    break

                evolver.apply_evolution(agent_name, result)
                change = result.get("change_description", "unknown")

                # Backtest after
                after = await bt.backtest_agent(agent_name, test_rounds)
                after_wr = after.get("win_rate", 0) if "error" not in after else 0

                delta = after_wr - before_wr
                if delta >= 0:
                    logger.info(f"    Iter {i+1}: KEPT — {change[:60]} (test: {before_wr:.1%} → {after_wr:.1%})")
                else:
                    # Revert
                    import shutil
                    agent_dir = os.path.join(AGENTS_DIR, agent_name)
                    prev = os.path.join(agent_dir, "strategy.md.prev")
                    curr = os.path.join(agent_dir, "strategy.md")
                    if os.path.exists(prev):
                        shutil.copy2(prev, curr)
                    logger.info(f"    Iter {i+1}: REVERTED — {change[:60]} (test: {before_wr:.1%} → {after_wr:.1%})")

    # Phase 5: PRUNE — mirror anti-predictive, retire hopeless
    logger.info("Phase 5: PRUNE — checking for mirrors and retirements")
    spawner = AgentSpawner(AGENTS_DIR)
    graveyard = os.path.join(DATA_DIR, "coordinator", "graveyard")

    for s in stats:
        if s["mirror"] or s["is_ensemble"]:
            continue
        if s["rounds"] < MIRROR_MIN_ROUNDS:
            continue

        # Auto-mirror extremely anti-predictive agents
        if s["win_rate"] < MIRROR_THRESHOLD_WR:
            existing_mirrors = [
                a["name"] for a in stats
                if a["mirror"] and s["name"].split("-", 2)[-1] in a["name"]
            ]
            if not existing_mirrors and total_agents < MAX_AGENTS:
                mirror = spawner.spawn_mirror(s["name"])
                logger.info(f"  Auto-mirror: {s['name']} ({s['win_rate']:.1%}) → {mirror}")
                total_agents += 1

    logger.info(f"  {total_agents} agents after pruning")

    # Phase 6: UPDATE INSIGHTS — refresh tournament-insights.md with latest data
    logger.info("Phase 6: UPDATE — refreshing shared insights")
    _update_tournament_insights(stats, rounds)

    # Phase 7: SUMMARY
    logger.info(f"\n  Cycle #{cycle_num} complete. Next cycle in {CYCLE_INTERVAL_SECONDS//60} minutes.")


def _update_tournament_insights(stats: list[dict], rounds: list[dict]):
    """Auto-update tournament-insights.md with latest leaderboard data."""
    proven = sorted(
        [s for s in stats if s["rounds"] >= 20],
        key=lambda s: s["win_rate"],
        reverse=True,
    )
    if not proven:
        return

    # Only update the performance tiers section (append current snapshot)
    insights_path = os.path.join(SHARED_DIR, "tournament-insights.md")
    if not os.path.exists(insights_path):
        return

    # Write a timestamped snapshot to shared knowledge
    import time as _time
    ts = _time.strftime("%Y%m%d-%H%M")
    snapshot_path = os.path.join(SHARED_DIR, f"leaderboard-snapshot-{ts}.md")
    if os.path.exists(snapshot_path):
        return  # Already written this minute

    lines = [f"# Leaderboard Snapshot — {ts} ({len(rounds)} rounds)\n"]
    for s in proven:
        tier = "T1" if s["win_rate"] > 0.55 else "T2" if s["win_rate"] > 0.50 else "T3" if s["win_rate"] > 0.45 else "T4"
        lines.append(f"- [{tier}] {s['name']}: {s['win_rate']:.1%} ({s['rounds']}r)")

    with open(snapshot_path, "w") as f:
        f.write("\n".join(lines))
    logger.info(f"  Written leaderboard snapshot to {snapshot_path}")


async def main():
    logger.info("Strategy Factory starting — continuous optimization loop")
    logger.info(f"  Project: {PROJECT_DIR}")
    logger.info(f"  Cycle interval: {CYCLE_INTERVAL_SECONDS}s")

    cycle = 1
    while True:
        try:
            await run_factory_cycle(cycle)
        except Exception as e:
            logger.error(f"Cycle #{cycle} failed: {e}", exc_info=True)

        cycle += 1
        logger.info(f"Sleeping {CYCLE_INTERVAL_SECONDS}s until next cycle...")
        await asyncio.sleep(CYCLE_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
