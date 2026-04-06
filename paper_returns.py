#!/usr/bin/env python3
import argparse
import json
import os

from src.io_utils import read_jsonl


def summarize_agent(agent_dir: str) -> dict | None:
    exec_path = os.path.join(agent_dir, "executions.jsonl")
    if not os.path.exists(exec_path):
        return None
    executions = read_jsonl(exec_path)
    if not executions:
        return None
    total = len(executions)
    wins = sum(1 for e in executions if e.get("correct"))
    total_cost = sum(float(e.get("entry_price", 0.0)) for e in executions)
    total_payout = sum(float(e.get("payout", 0.0)) for e in executions)
    total_pnl = total_payout - total_cost
    total_return_pct = (total_pnl / total_cost) if total_cost else 0.0
    avg_return = sum(float(e.get("return_pct", 0.0)) for e in executions) / total
    avg_pnl = sum(float(e.get("pnl_per_share", 0.0)) for e in executions) / total
    return {
        "agent": os.path.basename(agent_dir),
        "trades": total,
        "wins": wins,
        "win_rate": wins / total,
        "total_cost": total_cost,
        "total_payout": total_payout,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "avg_return_pct": avg_return,
        "avg_pnl_per_share": avg_pnl,
        "last_market": executions[-1].get("market_slug"),
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize Polymarket paper returns")
    parser.add_argument("--dir", required=True, help="Project directory (e.g. live-gpt54)")
    args = parser.parse_args()

    agents_dir = os.path.join(args.dir, "agents")
    rows = []
    for name in sorted(os.listdir(agents_dir)):
        agent_dir = os.path.join(agents_dir, name)
        if not os.path.isdir(agent_dir):
            continue
        summary = summarize_agent(agent_dir)
        if summary:
            rows.append(summary)

    rows.sort(key=lambda r: (r["total_return_pct"], r["total_pnl"], r["win_rate"]), reverse=True)
    portfolio = {
        "agents": len(rows),
        "trades": sum(r["trades"] for r in rows),
        "wins": sum(r["wins"] for r in rows),
        "win_rate": (
            sum(r["wins"] for r in rows) / sum(r["trades"] for r in rows)
            if rows and sum(r["trades"] for r in rows)
            else 0.0
        ),
        "total_cost": sum(r["total_cost"] for r in rows),
        "total_payout": sum(r["total_payout"] for r in rows),
        "total_pnl": sum(r["total_pnl"] for r in rows),
    }
    portfolio["total_return_pct"] = (
        portfolio["total_pnl"] / portfolio["total_cost"]
        if portfolio["total_cost"]
        else 0.0
    )
    print(json.dumps({"portfolio": portfolio, "agents": rows}, indent=2))


if __name__ == "__main__":
    main()
