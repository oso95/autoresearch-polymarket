#!/usr/bin/env python3
"""
Spawn an army of agent-014 clones with diverse mutations,
plus ensemble meta-agents that combine multiple strategies.
"""
import os
import sys
import shutil
import json

sys.path.insert(0, os.path.dirname(__file__))
from src.coordinator.spawner import AgentSpawner
from src.runner.ensemble import create_ensemble_agent

AGENTS_DIR = "./live-run/agents"
SOURCE_AGENT = "agent-014-clone-clone-derivatives-analyst"

# ============================================================
# PART 1: 15 CLONES OF AGENT-014 WITH DIVERSE MUTATIONS
# ============================================================
MUTATIONS = [
    {
        "note": "Tighten dual convergence thresholds: require taker < 0.60 AND OB < 0.20 (was 0.70/0.30) for higher selectivity",
        "suffix": "tight-convergence",
    },
    {
        "note": "Loosen dual convergence thresholds: accept taker < 0.80 AND OB < 0.40 for more frequent signals",
        "suffix": "loose-convergence",
    },
    {
        "note": "Add time-of-day awareness: track which hours produce more mean-reversion vs trending behavior",
        "suffix": "time-aware",
    },
    {
        "note": "Add volume-weighted confidence: scale confidence by current volume relative to average — high volume = higher confidence in contrarian signals",
        "suffix": "volume-weighted",
    },
    {
        "note": "Asymmetric thresholds: use tighter threshold for UP predictions (0.15%) vs DOWN predictions (0.25%) based on observed UP bias in data",
        "suffix": "asymmetric-thresholds",
    },
    {
        "note": "Add momentum confirmation: only take mean reversion trades when RSI-like indicator confirms oversold/overbought (consecutive candle count > 3)",
        "suffix": "momentum-confirmed",
    },
    {
        "note": "Reduce neutral zone noise: when no strong signal, predict 50/50 (abstain) instead of 51-52% weak predictions — only trade high-confidence setups",
        "suffix": "high-conviction-only",
    },
    {
        "note": "Add cross-market correlation: check if Polymarket odds are moving in the same direction as Binance price — divergence = stronger contrarian signal",
        "suffix": "cross-market",
    },
    {
        "note": "Faster SMA: use 10-candle SMA instead of 20 for quicker mean reversion signals, with proportionally tighter thresholds (0.10% base)",
        "suffix": "fast-sma-10",
    },
    {
        "note": "Slower SMA: use 50-candle SMA for more stable mean, with wider thresholds (0.40% base) — trades less frequently but with higher conviction",
        "suffix": "slow-sma-50",
    },
    {
        "note": "Add open interest divergence: if OI is rising while price is falling = new shorts opening = potential squeeze UP. OI falling while price rising = longs exiting = potential reversal DOWN",
        "suffix": "oi-divergence",
    },
    {
        "note": "Combine Yi Jing hexagram casting with the tiered system: use last 6 candles as hexagram lines, upper trigram determines prediction direction, then confirm with Tier 1-3 signals",
        "suffix": "yi-jing-hybrid",
    },
    {
        "note": "Add Fibonacci retracement: identify swing high/low from last 20 candles, check if price is near key Fibonacci levels (0.382, 0.618) — these levels act as additional mean reversion magnets",
        "suffix": "fibonacci-enhanced",
    },
    {
        "note": "Anti-herding filter: if all other agents in the tournament predicted the same direction last round AND were wrong, add contrarian weight against that direction",
        "suffix": "anti-herding",
    },
    {
        "note": "Adaptive thresholds: instead of fixed 0.20% base threshold, use ATR-based threshold (e.g., 0.3 * ATR-5) that automatically adjusts to current volatility",
        "suffix": "adaptive-atr-threshold",
    },
]

print("=" * 60)
print("  SPAWNING CLONE ARMY + ENSEMBLES")
print("=" * 60)

spawner = AgentSpawner(AGENTS_DIR)

# Verify source agent exists
source_dir = os.path.join(AGENTS_DIR, SOURCE_AGENT)
if not os.path.isdir(source_dir):
    print(f"ERROR: Source agent {SOURCE_AGENT} not found!")
    sys.exit(1)

print(f"\nSource: {SOURCE_AGENT}")
print(f"Spawning {len(MUTATIONS)} clones...\n")

clone_names = []
for i, mutation in enumerate(MUTATIONS):
    clone_name = spawner.clone_agent(SOURCE_AGENT, mutation["note"])
    clone_names.append(clone_name)
    print(f"  [{i+1:2d}] {clone_name}")
    print(f"       Mutation: {mutation['note'][:70]}...")

print(f"\n{len(clone_names)} clones spawned!")

# ============================================================
# PART 2: ENSEMBLE META-AGENTS
# ============================================================
print("\n" + "=" * 60)
print("  CREATING ENSEMBLE META-AGENTS")
print("=" * 60)

# Get top agents by win rate for ensemble composition
from src.io_utils import read_jsonl
agent_stats = []
for name in sorted(os.listdir(AGENTS_DIR)):
    if not name.startswith("agent-"):
        continue
    preds = read_jsonl(os.path.join(AGENTS_DIR, name, "predictions.jsonl"))
    scored = [p for p in preds if p.get("correct") is not None]
    if scored:
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored)
        agent_stats.append((name, wr, len(scored)))

agent_stats.sort(key=lambda x: x[1], reverse=True)
print("\nTop agents for ensemble selection:")
for name, wr, n in agent_stats[:10]:
    print(f"  {name}: {wr:.0%} ({n} rounds)")

# Select diverse agents for ensembles (by approach type, not just win rate)
top_derivative = "agent-014-clone-clone-derivatives-analyst"  # 75% — derivatives/taker contrarian
top_contrarian = "agent-004-contrarian"  # 53% — Polymarket consensus + dual convergence
top_regime = "agent-010-regime-detector"  # 53% — ATR regime + graduated slope
top_mean_rev = "agent-007-mean-reversion"  # 49% — pure SMA mean reversion
top_volume = "agent-006-volume-spike-detector"  # 49% — volume spike + asymmetric taker
top_orderbook = "agent-001-orderbook-specialist"  # 44% — convergent OB contrarian
top_clone = "agent-012-clone-derivatives-analyst"  # 68% — derivatives clone

ensemble_configs = [
    # 2-agent ensembles
    {
        "name": "top2-derivative-contrarian",
        "members": [top_derivative, top_contrarian],
        "desc": "Best derivative (taker/OB contrarian) + best contrarian (Polymarket fade). Different signal sources = high diversity.",
    },
    {
        "name": "top2-derivative-regime",
        "members": [top_derivative, top_regime],
        "desc": "Best derivative + regime detector. Derivatives read flow, regime reads volatility structure.",
    },
    {
        "name": "top2-clones",
        "members": [top_derivative, top_clone],
        "desc": "Both top derivatives clones. Similar approach but different evolution paths — tests if consensus within a family improves accuracy.",
    },
    # 3-agent ensembles
    {
        "name": "top3-diverse",
        "members": [top_derivative, top_contrarian, top_regime],
        "desc": "Top 3 by win rate from DIFFERENT strategy families. Maximum signal diversity.",
    },
    {
        "name": "top3-all-derivatives",
        "members": [top_derivative, top_clone, "agent-003-derivatives-analyst"],
        "desc": "All 3 derivatives-family agents. Tests if family consensus adds value.",
    },
    {
        "name": "top3-anti-correlated",
        "members": [top_derivative, top_mean_rev, top_volume],
        "desc": "Picks agents that are likely to disagree: derivatives (flow), mean reversion (structure), volume (spikes). Disagreement = high value when majority wins.",
    },
    # 5-agent ensembles
    {
        "name": "top5-diverse",
        "members": [top_derivative, top_contrarian, top_regime, top_mean_rev, top_volume],
        "desc": "Top 5 diverse approaches. Maximum breadth — flow, consensus, regime, structure, volume.",
    },
    {
        "name": "top5-best-wr",
        "members": [top_derivative, top_clone, top_contrarian, top_regime, "agent-003-derivatives-analyst"],
        "desc": "Top 5 by raw win rate. Quality over diversity.",
    },
    # 7-agent ensemble
    {
        "name": "top7-full",
        "members": [top_derivative, top_clone, top_contrarian, top_regime, top_mean_rev, top_volume, top_orderbook],
        "desc": "7 agents covering all strategy types. Most robust ensemble — hard to be unanimously wrong.",
    },
    # All-agent ensemble
    {
        "name": "all-agents",
        "members": [s[0] for s in agent_stats if s[2] >= 10],  # Only agents with 10+ rounds
        "desc": "Every agent with 10+ scored rounds votes. Maximum information aggregation.",
    },
]

ensemble_names = []
for config in ensemble_configs:
    name = create_ensemble_agent(
        AGENTS_DIR,
        config["name"],
        config["members"],
        weighting="win-rate",
        description=config["desc"],
    )
    ensemble_names.append(name)
    print(f"\n  Ensemble: {name}")
    print(f"  Members: {len(config['members'])} agents")
    print(f"  {config['desc'][:80]}")

print(f"\n{len(ensemble_names)} ensembles created!")

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("  ARMY SPAWNED")
print("=" * 60)
total_agents = len(os.listdir(AGENTS_DIR))
print(f"\n  {len(clone_names)} clones of agent-014")
print(f"  {len(ensemble_names)} ensemble meta-agents")
print(f"  {total_agents} total agents in tournament")
print(f"\n  Ready to run!")
