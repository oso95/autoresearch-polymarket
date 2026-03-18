#!/usr/bin/env python3
"""Quick dashboard — shows current system state at a glance."""
import json
import os
import sys
import time

def main():
    project_dir = sys.argv[1] if len(sys.argv) > 1 else "./live-run"
    data_dir = os.path.join(project_dir, "data")
    agents_dir = os.path.join(project_dir, "agents")

    print("=" * 70)
    print("  POLYMARKET BTC 5m STRATEGY DISCOVERY — DASHBOARD")
    print("=" * 70)

    # System health
    hb_path = os.path.join(data_dir, "live", "heartbeat.json")
    if os.path.exists(hb_path):
        hb = json.load(open(hb_path))
        age = (time.time() * 1000 - hb["timestamp"]) / 1000
        status = "HEALTHY" if age < 30 else f"STALE ({age:.0f}s)"
    else:
        status = "NOT RUNNING"
    print(f"\nSystem: {status}")

    # Rounds
    rounds_dir = os.path.join(data_dir, "rounds")
    rounds = sorted([d for d in os.listdir(rounds_dir) if d.isdigit()]) if os.path.isdir(rounds_dir) else []
    print(f"Rounds: {len(rounds)}")

    # Agent leaderboard
    print(f"\n{'Agent':<35} {'Pred':>5} {'Scored':>7} {'Wins':>5} {'WR':>6} {'Streak':>7}")
    print("-" * 70)

    agent_stats = []
    for name in sorted(os.listdir(agents_dir)):
        agent_dir = os.path.join(agents_dir, name)
        if not os.path.isdir(agent_dir):
            continue
        pred_path = os.path.join(agent_dir, "predictions.jsonl")
        if not os.path.exists(pred_path):
            continue
        preds = []
        with open(pred_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    preds.append(json.loads(line))
        scored = [p for p in preds if p.get("correct") is not None]
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored) * 100 if scored else 0
        # Streak
        streak = 0
        streak_type = ""
        for p in reversed(scored):
            if not streak:
                streak_type = "W" if p["correct"] else "L"
                streak = 1
            elif (p["correct"] and streak_type == "W") or (not p["correct"] and streak_type == "L"):
                streak += 1
            else:
                break
        streak_str = f"{streak}{streak_type}" if streak else "-"
        agent_stats.append((name, len(preds), len(scored), wins, wr, streak_str))

    agent_stats.sort(key=lambda x: x[4], reverse=True)
    for name, total, scored, wins, wr, streak in agent_stats:
        short_name = name[:34]
        print(f"{short_name:<35} {total:>5} {scored:>7} {wins:>5} {wr:>5.0f}% {streak:>7}")

    # Evolution history
    print(f"\n--- Evolution History ---")
    for name in sorted(os.listdir(agents_dir)):
        results_path = os.path.join(agents_dir, name, "results.tsv")
        if os.path.exists(results_path):
            with open(results_path) as f:
                lines = f.readlines()
            if len(lines) > 1:
                for line in lines[1:]:
                    parts = line.strip().split("\t")
                    if len(parts) >= 7:
                        print(f"  {name}: {parts[6][:60]}...")

    # Shared knowledge
    shared_dir = os.path.join(data_dir, "shared_knowledge")
    if os.path.isdir(shared_dir):
        files = [f for f in os.listdir(shared_dir) if f.startswith("discovery-")]
        if files:
            print(f"\n--- Shared Discoveries ({len(files)}) ---")
            for f in sorted(files)[-5:]:
                print(f"  {f}")

    print()

if __name__ == "__main__":
    main()
