# src/runner/evolver.py
"""
Strategy Evolution Loop — the autoresearch inner loop.

After every K rounds, each agent reviews its performance and evolves its strategy.
This is Phases 1-3 of the adapted 10-phase protocol:
  Phase 1: REVIEW — analyze own results + shared ledger
  Phase 2: IDEATE — what worked, what didn't, what to try
  Phase 3: MODIFY — make one atomic change to strategy.md or scripts

The agent is free to:
  - Rewrite sections of strategy.md
  - Create new analysis scripts
  - Modify existing scripts
  - Change decision thresholds
  - Try entirely new approaches

The evolution prompt gives the agent full context and asks it to output
the updated strategy.md and optionally new/modified scripts.
"""
import asyncio
import json
import logging
import os
import shutil
import re

from src.io_utils import read_jsonl

logger = logging.getLogger(__name__)


def _build_evolution_prompt(
    agent_name: str,
    strategy: str,
    scripts: dict[str, str],
    predictions: list[dict],
    notes: str,
    shared_ledger_summary: str,
    leaderboard_summary: str,
) -> str:
    # Build win/loss record
    scored = [p for p in predictions if p.get("correct") is not None]
    total = len(scored)
    wins = sum(1 for p in scored if p["correct"])
    win_rate = wins / total if total > 0 else 0.0
    recent = scored[-20:]

    # Build per-round detail
    round_details = []
    for p in recent:
        result = "W" if p["correct"] else "L"
        round_details.append(
            f"  Round {p['round']}: predicted {p['prediction']}, actual {p['outcome']} → {result} "
            f"(confidence {p.get('confidence', '?')}, reasoning: {p.get('reasoning', '?')[:100]})"
        )

    parts = [
        f"# Strategy Evolution for {agent_name}",
        "",
        "You are an autonomous trading strategy agent participating in a tournament.",
        "Your job is to EVOLVE your strategy to improve your win rate on Polymarket's",
        "5-minute BTC prediction markets (Up or Down).",
        "",
        f"## Your Current Performance",
        f"- Win rate: {win_rate:.1%} ({wins}/{total})",
        f"- Recent record: {', '.join('W' if p['correct'] else 'L' for p in recent[-10:])}",
        "",
        "## Your Recent Prediction Detail",
        "\n".join(round_details) if round_details else "  No predictions yet",
        "",
        "## Your Current Strategy",
        "```markdown",
        strategy,
        "```",
        "",
    ]

    if scripts:
        parts.append("## Your Current Scripts")
        for name, code in scripts.items():
            parts.extend([f"### {name}", f"```python\n{code}\n```", ""])

    if notes:
        parts.extend(["## Notes & Coordinator Suggestions", notes, ""])

    if shared_ledger_summary:
        parts.extend(["## What Other Agents Are Doing", shared_ledger_summary, ""])

    if leaderboard_summary:
        parts.extend(["## Tournament Leaderboard", leaderboard_summary, ""])

    parts.extend([
        "## Your Task: EVOLVE",
        "",
        "Analyze your results. Ask yourself:",
        "- What patterns do my wins share? What about my losses?",
        "- Am I using the right data sources? Am I ignoring useful signals?",
        "- Are my thresholds too aggressive or too conservative?",
        "- Did the coordinator suggest anything worth trying?",
        "- What are the top-performing agents doing differently?",
        "",
        "Then make **ONE focused change** to improve your strategy.",
        "This could be:",
        "- Adjusting a threshold or weight",
        "- Adding a new signal or data source",
        "- Changing your decision logic",
        "- Creating a new analysis script",
        "- Completely rethinking your approach if win rate is bad",
        "",
        "## Output Format",
        "",
        "Respond with a JSON object containing your changes:",
        "```json",
        "{",
        '  "change_description": "one sentence describing what you changed and why",',
        '  "strategy_md": "your complete updated strategy.md content",',
        '  "new_scripts": {',
        '    "script_name.py": "script content"',
        '  },',
        '  "delete_scripts": ["old_script.py"]',
        "}",
        "```",
        "",
        "IMPORTANT:",
        "- strategy_md must be the COMPLETE updated strategy, not a diff",
        "- new_scripts can add or overwrite scripts (use same name to overwrite)",
        "- delete_scripts lists scripts to remove (optional)",
        "- Make ONE focused change, not many changes at once",
        "- If your win rate is good (>55%), make small refinements",
        "- If your win rate is bad (<45%), consider bigger changes",
        "- If you have no data yet, keep your current strategy but add notes about what to watch for",
    ])

    return "\n".join(parts)


def _build_shared_ledger_summary(agents_dir: str, current_agent: str) -> str:
    """Summarize what other agents predicted and their accuracy."""
    lines = []
    if not os.path.isdir(agents_dir):
        return ""
    for name in sorted(os.listdir(agents_dir)):
        if name == current_agent or not name.startswith("agent-"):
            continue
        pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
        preds = read_jsonl(pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        if not scored:
            continue
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored) if scored else 0
        recent = scored[-5:]
        recent_str = ", ".join("W" if p["correct"] else "L" for p in recent)
        lines.append(f"- {name}: {wr:.0%} win rate ({wins}/{len(scored)}), recent: {recent_str}")
    return "\n".join(lines) if lines else "No other agent data available yet."


def _build_leaderboard_summary(data_dir: str) -> str:
    """Read current leaderboard and format as text."""
    lb_path = os.path.join(data_dir, "coordinator", "leaderboard.json")
    if not os.path.exists(lb_path):
        return "No leaderboard yet."
    try:
        with open(lb_path) as f:
            lb = json.load(f)
        entries = lb.get("entries", [])
        if not entries:
            return "No leaderboard yet."
        lines = ["| Rank | Agent | Win Rate | Rounds | Status |",
                 "|------|-------|----------|--------|--------|"]
        for i, e in enumerate(entries, 1):
            status = "PROVEN" if e.get("proven") else ""
            lines.append(f"| {i} | {e['agent_name']} | {e['win_rate']:.1%} | {e['total_rounds']} | {status} |")
        return "\n".join(lines)
    except Exception:
        return "Leaderboard unavailable."


def _parse_evolution_response(response: str) -> dict | None:
    """Parse the evolution response from Claude."""
    # Try direct JSON parse
    try:
        data = json.loads(response)
        if "strategy_md" in data:
            return data
    except json.JSONDecodeError:
        pass

    # Try to find JSON block in response
    json_match = re.search(r'```json\s*\n(.*?)\n```', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if "strategy_md" in data:
                return data
        except json.JSONDecodeError:
            pass

    # Try to find any JSON object with strategy_md
    json_match = re.search(r'\{[^{}]*"strategy_md".*\}', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if "strategy_md" in data:
                return data
        except json.JSONDecodeError:
            pass

    return None


class StrategyEvolver:
    # Use sonnet for evolution — needs reasoning but shouldn't be too slow
    EVOLUTION_MODEL = "sonnet"

    def __init__(self, agents_dir: str, data_dir: str, timeout_seconds: int = 120):
        self.agents_dir = agents_dir
        self.data_dir = data_dir
        self.timeout = timeout_seconds

    async def evolve_agent(self, agent_name: str) -> dict | None:
        """
        Run the autoresearch inner loop for one agent:
        REVIEW → IDEATE → MODIFY (all in one Claude invocation).

        Returns the evolution result or None if failed.
        """
        agent_dir = os.path.join(self.agents_dir, agent_name)

        # Read current strategy
        strategy_path = os.path.join(agent_dir, "strategy.md")
        if not os.path.exists(strategy_path):
            return None
        with open(strategy_path) as f:
            strategy = f.read()

        # Read scripts
        scripts = {}
        scripts_dir = os.path.join(agent_dir, "scripts")
        if os.path.isdir(scripts_dir):
            for fname in sorted(os.listdir(scripts_dir)):
                fpath = os.path.join(scripts_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        scripts[fname] = f.read()

        # Read predictions
        predictions = read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))

        # Read notes
        notes = ""
        notes_path = os.path.join(agent_dir, "notes.md")
        if os.path.exists(notes_path):
            with open(notes_path) as f:
                notes = f.read()

        # Read shared knowledge
        shared_knowledge_dir = os.path.join(self.data_dir, "shared_knowledge")
        if os.path.isdir(shared_knowledge_dir):
            for fname in sorted(os.listdir(shared_knowledge_dir)):
                fpath = os.path.join(shared_knowledge_dir, fname)
                if os.path.isfile(fpath) and fname.endswith((".md", ".txt")):
                    with open(fpath) as f:
                        notes += f"\n\n## Shared Knowledge: {fname}\n{f.read()}"

        # Build context
        shared_summary = _build_shared_ledger_summary(self.agents_dir, agent_name)
        lb_summary = _build_leaderboard_summary(self.data_dir)

        prompt = _build_evolution_prompt(
            agent_name, strategy, scripts, predictions,
            notes, shared_summary, lb_summary,
        )

        # Invoke Claude for strategy evolution (use sonnet for deeper reasoning)
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "claude", "-p", prompt, "--output-format", "text",
                    "--model", self.EVOLUTION_MODEL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=agent_dir,
                ),
                timeout=self.timeout,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
            response = stdout.decode("utf-8").strip()

            if proc.returncode != 0:
                logger.warning(f"Evolution failed for {agent_name}: {stderr.decode()[:200]}")
                return None

            result = _parse_evolution_response(response)
            if result is None:
                logger.warning(f"Could not parse evolution response for {agent_name}")
                return None

            return result

        except asyncio.TimeoutError:
            logger.warning(f"Evolution timed out for {agent_name}")
            return None
        except Exception as e:
            logger.error(f"Evolution failed for {agent_name}: {e}")
            return None

    def apply_evolution(self, agent_name: str, result: dict) -> bool:
        """
        Apply the evolution result to the agent's workspace.
        Backs up the old strategy for fast-fail revert.
        """
        agent_dir = os.path.join(self.agents_dir, agent_name)
        strategy_path = os.path.join(agent_dir, "strategy.md")
        prev_path = os.path.join(agent_dir, "strategy.md.prev")

        # Phase 4: SNAPSHOT — backup current strategy
        if os.path.exists(strategy_path):
            shutil.copy2(strategy_path, prev_path)

        # Write new strategy
        new_strategy = result.get("strategy_md", "")
        if not new_strategy:
            logger.warning(f"Empty strategy_md in evolution result for {agent_name}")
            return False

        with open(strategy_path, "w") as f:
            f.write(new_strategy)

        # Handle scripts
        scripts_dir = os.path.join(agent_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)

        # Add/update scripts
        for name, content in result.get("new_scripts", {}).items():
            script_path = os.path.join(scripts_dir, name)
            with open(script_path, "w") as f:
                f.write(content)

        # Delete scripts
        for name in result.get("delete_scripts", []):
            script_path = os.path.join(scripts_dir, name)
            if os.path.exists(script_path):
                os.unlink(script_path)

        change = result.get("change_description", "no description")
        logger.info(f"Evolved {agent_name}: {change}")

        # Append to results.tsv
        results_path = os.path.join(agent_dir, "results.tsv")
        scored = [p for p in read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
                  if p.get("correct") is not None]
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored) if scored else 0.0
        with open(results_path, "a") as f:
            f.write(f"{len(scored)}\t{hash(new_strategy) & 0xFFFFFFFF:08x}\t{wr:.3f}\t0\t{len(scored)}\tevolve\t{change}\n")

        return True
