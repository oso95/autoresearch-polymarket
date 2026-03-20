#!/usr/bin/env python3
"""
Wave 2: Mirror agents (invert worst performers) + Creative strategies + Model experiments.

Addresses open questions:
1. Do mirror/reversed agents (inverting worst performers) actually work?
2. Can creative strategies (Yi Jing, Tarot, Numerology, Astro) find edges?
3. Does using a stronger GPT model for predictions improve win rate?
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))
from src.codex_cli import DEFAULT_PREDICTION_MODEL
from src.coordinator.spawner import AgentSpawner, SEED_STRATEGIES
from src.io_utils import read_jsonl

AGENTS_DIR = "./live-run/agents"

spawner = AgentSpawner(AGENTS_DIR)

print("=" * 60)
print("  WAVE 2: MIRRORS + CREATIVE + MODEL EXPERIMENTS")
print("=" * 60)

# ============================================================
# PART 1: MIRROR AGENTS — Invert worst performers
# ============================================================
print("\n--- PART 1: MIRROR AGENTS ---")
print("Hypothesis: If an agent is consistently wrong, inverting its")
print("signal should be consistently right.\n")

# Find agents with lowest win rates (minimum 20 rounds for statistical significance)
agent_stats = []
for name in sorted(os.listdir(AGENTS_DIR)):
    if not name.startswith("agent-") or "ensemble" in name or "mirror" in name:
        continue
    preds = read_jsonl(os.path.join(AGENTS_DIR, name, "predictions.jsonl"))
    scored = [p for p in preds if p.get("correct") is not None]
    if len(scored) >= 20:
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored)
        agent_stats.append((name, wr, len(scored)))

agent_stats.sort(key=lambda x: x[1])

print("Worst performers (candidates for mirroring):")
for name, wr, n in agent_stats[:8]:
    print(f"  {name}: {wr:.1%} ({n} rounds)")

# Mirror the worst 5 agents (below 46%)
mirror_candidates = [(n, wr, total) for n, wr, total in agent_stats if wr < 0.46]
print(f"\nMirroring {len(mirror_candidates)} agents (all below 46% win rate):")

mirror_names = []
for name, wr, total in mirror_candidates:
    mirror_name = spawner.spawn_mirror(name)
    mirror_names.append(mirror_name)
    expected_wr = 1.0 - wr
    print(f"  {mirror_name}")
    print(f"    Source: {name} ({wr:.1%} WR) → Expected mirror: ~{expected_wr:.1%}")

print(f"\n{len(mirror_names)} mirror agents spawned!")

# ============================================================
# PART 2: CREATIVE STRATEGY AGENTS
# ============================================================
print("\n--- PART 2: CREATIVE STRATEGIES ---")
print("Deploying unconventional approaches that may find non-obvious patterns.\n")

# Find creative seeds by name
creative_seed_names = [
    "yi-jing-oracle",
    "fibonacci-spiral",
    "crowd-psychology",
    "tarot-arcana",
    "gematria-numerology",
    "astro-cycles",
]

creative_names = []
for seed_name in creative_seed_names:
    seed = next((s for s in SEED_STRATEGIES if s["name"] == seed_name), None)
    if seed is None:
        print(f"  WARNING: Seed '{seed_name}' not found in SEED_STRATEGIES, skipping")
        continue
    agent_name = spawner.spawn_from_seed(seed)
    creative_names.append(agent_name)
    print(f"  Spawned: {agent_name}")

print(f"\n{len(creative_names)} creative agents spawned!")

# ============================================================
# PART 3: MODEL POLICY
# ============================================================
print("\n--- PART 3: MODEL EXPERIMENTS ---")
print("Single-model policy is active: all agents use the same GPT model.\n")

model_experiment_names = []
print(f"  All model-backed agents use {DEFAULT_PREDICTION_MODEL}")
print("  No alternate-model experiment agents are spawned")

print(f"\n{len(model_experiment_names)} model experiments set up!")

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("  WAVE 2 COMPLETE")
print("=" * 60)

total_new = len(mirror_names) + len(creative_names) + len(model_experiment_names)
total_agents = len([d for d in os.listdir(AGENTS_DIR) if d.startswith("agent-")])

print(f"\n  {len(mirror_names)} mirror agents (inverted worst performers)")
print(f"  {len(creative_names)} creative strategy agents")
print(f"  {len(model_experiment_names)} model experiment agents")
print(f"  = {total_new} new agents added")
print(f"\n  {total_agents} total agents now in tournament")
print(f"\n  Open questions being tested:")
print(f"    1. Mirror agents: Do inverted signals beat the originals?")
print(f"    2. Creative strategies: Can Yi Jing/Tarot/Astro find edges?")
print(f"    3. Strategy quality under a fixed {DEFAULT_PREDICTION_MODEL} runtime")
print(f"\n  Run the system to start testing!")
