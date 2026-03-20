# src/runner/ensemble.py
"""
Ensemble Meta-Agent — combines predictions from multiple strategy agents
via weighted voting to produce a single higher-confidence prediction.

Ensemble types:
- 2-agent: best pair
- 3-agent: top 3
- 5-agent: top 5
- all-agent: everyone votes

Weighting options:
- equal: every agent gets 1 vote
- win-rate: agents weighted by their historical win rate
- confidence: agents weighted by their stated confidence
- combined: win_rate * confidence
"""
import json
import os
import logging
from src.io_utils import read_jsonl
from src.memory_utils import init_memory

logger = logging.getLogger(__name__)


def get_agent_win_rates(agents_dir: str) -> dict[str, float]:
    """Get win rates for all agents."""
    rates = {}
    for name in sorted(os.listdir(agents_dir)):
        pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
        preds = read_jsonl(pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        if scored:
            wins = sum(1 for p in scored if p["correct"])
            rates[name] = wins / len(scored)
        else:
            rates[name] = 0.5  # default for new agents
    return rates


def build_ensemble_strategy(
    ensemble_name: str,
    member_agents: list[str],
    weighting: str = "win-rate",
    description: str = "",
) -> str:
    """Build a strategy.md for an ensemble meta-agent."""
    members_list = "\n".join(f"- `{a}`" for a in member_agents)

    return f"""# Ensemble: {ensemble_name}

## Type
Meta-agent that combines predictions from {len(member_agents)} strategy agents via weighted voting.

## Member Agents
{members_list}

## Weighting Method
**{weighting}** — {"each agent weighted by their historical win rate" if weighting == "win-rate" else "each agent weighted equally" if weighting == "equal" else "agents weighted by win_rate * stated_confidence"}

## How It Works
1. Read the current market snapshot
2. For each member agent, read their strategy.md and apply their decision logic
3. Each agent produces a prediction (Up/Down) with a confidence level
4. Weight each prediction by the agent's win rate (or equal weight)
5. Sum weighted votes for Up vs Down
6. Predict whichever direction has higher weighted votes
7. Confidence = winning_weight / total_weight

## Key Principle
Ensemble diversity is critical — combining agents that use DIFFERENT signals
(order book, taker flow, mean reversion, regime detection) produces better
results than combining agents with the same approach.

{description}

## Decision Process
When analyzing the market snapshot:
1. Apply each member agent's strategy independently
2. Record each prediction and confidence
3. Weight by win rate: higher-performing agents get more influence
4. The ensemble prediction is the weighted majority vote
5. If vote is very close (within 5%), reduce confidence to 51%

## Fallback
If unable to apply a member's strategy, skip that member and vote with remaining members.
"""


def create_ensemble_agent(
    agents_dir: str,
    ensemble_name: str,
    member_agents: list[str],
    weighting: str = "win-rate",
    description: str = "",
) -> str:
    """Create an ensemble agent directory with strategy."""
    # Find next ID
    existing = []
    for name in os.listdir(agents_dir):
        if name.startswith("agent-"):
            try:
                id_part = int(name.split("-")[1])
                existing.append(id_part)
            except (ValueError, IndexError):
                pass
    next_id = max(existing, default=0) + 1

    agent_name = f"agent-{next_id:03d}-ensemble-{ensemble_name}"
    agent_dir = os.path.join(agents_dir, agent_name)
    os.makedirs(agent_dir, exist_ok=True)
    os.makedirs(os.path.join(agent_dir, "scripts"), exist_ok=True)

    # Write strategy
    strategy = build_ensemble_strategy(ensemble_name, member_agents, weighting, description)
    with open(os.path.join(agent_dir, "strategy.md"), "w") as f:
        f.write(strategy)

    # Write member list as JSON for easy parsing
    with open(os.path.join(agent_dir, "ensemble_members.json"), "w") as f:
        json.dump({
            "members": member_agents,
            "weighting": weighting,
        }, f, indent=2)

    # Copy member strategies into scripts/ for reference
    for member in member_agents:
        member_strategy = os.path.join(agents_dir, member, "strategy.md")
        if os.path.exists(member_strategy):
            with open(member_strategy) as src:
                content = src.read()
            dest = os.path.join(agent_dir, "scripts", f"strategy_{member}.md")
            with open(dest, "w") as dst:
                dst.write(content)

    # Notes
    with open(os.path.join(agent_dir, "notes.md"), "w") as f:
        f.write(f"# Ensemble: {ensemble_name}\n\n")
        f.write(f"Combines {len(member_agents)} agents via {weighting} weighted voting.\n")
        f.write(f"Members: {', '.join(member_agents)}\n")
    init_memory(
        agent_dir,
        agent_name,
        origin="ensemble",
        change=f"Initialized ensemble `{ensemble_name}` with {len(member_agents)} members.",
        why=f"Combine agents through {weighting} weighted voting.",
        version="v1.0",
    )

    # Results TSV
    with open(os.path.join(agent_dir, "results.tsv"), "w") as f:
        f.write("iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n")

    logger.info(f"Created ensemble {agent_name} with {len(member_agents)} members")
    return agent_name
