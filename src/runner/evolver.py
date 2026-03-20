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

from src.codex_cli import DEFAULT_EVOLUTION_MODEL, run_codex_prompt
from src.io_utils import atomic_write_json, read_jsonl
from src.memory_utils import (
    append_memory_entry,
    append_memory_outcome,
    current_memory_version,
    next_memory_version,
    read_memory_bundle,
    set_current_memory_version,
)
from src.shared_knowledge import SharedKnowledgeForum, build_shared_knowledge_context, ensure_shared_knowledge_forum

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
        "- Prefer deterministic Python scripts in `scripts/` when a reusable signal can be computed mechanically",
        "- The runtime executes `.py` scripts before falling back to the model, so script-first edges are preferred",
        "",
        "## Output Format",
        "",
        "Do not run shell commands or modify files yourself. Respond with JSON only.",
        "",
        "Respond with a JSON object containing your changes:",
        "```json",
        "{",
        '  "change_description": "one sentence describing what you changed and why",',
        '  "change_summary": "short description of what changed",',
        '  "change_why": "short explanation of why this change was chosen",',
        '  "strategy_md": "your complete updated strategy.md content",',
        '  "new_scripts": {',
        '    "script_name.py": "script content"',
        '  },',
        '  "delete_scripts": ["old_script.py"],',
        '  "shared_discovery_title": "optional short title for a forum post",',
        '  "shared_discovery": "optional: if you found something useful that other agents should know, write it here"',
        '  ,"shared_votes": [{"post_id": "optional forum post id", "vote": "up or down", "reason": "optional short reason"}]',
        '  ,"shared_comments": [{"post_id": "optional forum post id", "comment": "optional short comment"}]',
        "}",
        "```",
        "",
        "IMPORTANT:",
        "- strategy_md must be the COMPLETE updated strategy, not a diff",
        "- new_scripts can add or overwrite scripts (use same name to overwrite)",
        "- delete_scripts lists scripts to remove (optional)",
        "- shared_discovery_title and shared_discovery create a new forum post in the shared knowledge base.",
        "- Good forum posts are short and testable: title + claim + evidence + caveat.",
        "- shared_votes lets you upvote/downvote existing shared knowledge posts after reviewing them.",
        "- shared_comments lets you leave short comments on existing posts.",
        "- Do not dump raw logs into shared_discovery. Summarize the key reusable lesson instead.",
        "- change_summary and change_why should be concise because they are written into your long-term memory file",
        "- Make ONE focused change, not many changes at once",
        "- If your win rate is good (>55%), make small refinements",
        "- If your win rate is bad (<45%), consider bigger changes",
        "- If you have no data yet, keep your current strategy but add notes about what to watch for",
        "- AVOID OVERFITTING: Don't create rules that only work for specific price levels or specific",
        "  time periods. Focus on structural patterns (like 'mean reversion after extreme moves') rather",
        "  than specific numbers. Your strategy will be validated on unseen data.",
        "- Think about REGIME ROBUSTNESS: Does your strategy work in trending markets, ranging markets,",
        "  and volatile markets? If it only works in one regime, add regime detection.",
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
    """Parse the evolution response from the model."""
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
    # Use a stronger GPT model for evolution than the default live fallback.
    EVOLUTION_MODEL = DEFAULT_EVOLUTION_MODEL

    def __init__(self, agents_dir: str, data_dir: str, timeout_seconds: int = 180, evaluation_window: int = 5):
        self.agents_dir = agents_dir
        self.data_dir = data_dir
        self.timeout = timeout_seconds
        self.evaluation_window = evaluation_window

    def _status_path(self, agent_name: str) -> str:
        return os.path.join(self.agents_dir, agent_name, "status.json")

    def _read_status(self, agent_name: str) -> dict:
        path = self._status_path(agent_name)
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_status(self, agent_name: str, status: dict):
        status["updated_at"] = status.get("updated_at") or int(__import__("time").time() * 1000)
        atomic_write_json(self._status_path(agent_name), status)

    def _scripts_backup_dir(self, agent_dir: str) -> str:
        return os.path.join(agent_dir, "scripts.prev")

    def _snapshot_scripts(self, agent_dir: str):
        scripts_dir = os.path.join(agent_dir, "scripts")
        backup_dir = self._scripts_backup_dir(agent_dir)
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        if os.path.isdir(scripts_dir):
            shutil.copytree(scripts_dir, backup_dir)
        else:
            os.makedirs(backup_dir, exist_ok=True)

    def _restore_previous_state(self, agent_dir: str):
        strategy_path = os.path.join(agent_dir, "strategy.md")
        prev_path = os.path.join(agent_dir, "strategy.md.prev")
        scripts_dir = os.path.join(agent_dir, "scripts")
        backup_dir = self._scripts_backup_dir(agent_dir)
        if os.path.exists(prev_path):
            shutil.copy2(prev_path, strategy_path)
        if os.path.exists(scripts_dir):
            shutil.rmtree(scripts_dir)
        if os.path.isdir(backup_dir):
            shutil.copytree(backup_dir, scripts_dir)
        else:
            os.makedirs(scripts_dir, exist_ok=True)

    def _append_results_row(
        self,
        agent_name: str,
        iteration: int,
        strategy_version: str,
        win_rate: float,
        delta: float,
        rounds_played: int,
        status: str,
        description: str,
    ):
        results_path = os.path.join(self.agents_dir, agent_name, "results.tsv")
        with open(results_path, "a") as f:
            f.write(
                f"{iteration}\t{strategy_version}\t{win_rate:.3f}\t{delta:.3f}\t"
                f"{rounds_played}\t{status}\t{description}\n"
            )

    def finalize_pending_experiment(self, agent_name: str) -> dict | None:
        status = self._read_status(agent_name)
        experiment = status.get("current_experiment")
        if not experiment:
            return None

        pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        scored = [p for p in read_jsonl(pred_path) if p.get("correct") is not None]
        eval_rounds = [
            p for p in scored
            if p["round"] > experiment.get("applied_at_round", -1)
        ][:self.evaluation_window]

        if len(eval_rounds) < self.evaluation_window:
            return {"status": "pending", "rounds": len(eval_rounds)}

        new_wr = sum(1 for p in eval_rounds if p["correct"]) / len(eval_rounds)
        baseline = experiment.get("baseline_win_rate", 0.0)
        delta = new_wr - baseline
        keep = new_wr > baseline
        agent_dir = os.path.join(self.agents_dir, agent_name)
        previous_version = experiment.get("previous_memory_version", "v1.0")
        experiment_version = experiment.get("memory_version", previous_version)
        if not keep:
            self._restore_previous_state(agent_dir)
            set_current_memory_version(agent_dir, previous_version)

        strategy_path = os.path.join(agent_dir, "strategy.md")
        strategy_version = "unknown"
        if os.path.exists(strategy_path):
            with open(strategy_path) as f:
                strategy_version = f"{hash(f.read()) & 0xFFFFFFFF:08x}"

        status["last_action"] = "keep" if keep else "discard"
        status["last_action_round"] = eval_rounds[-1]["round"]
        status["current_experiment"] = None
        status["consecutive_discards"] = 0 if keep else status.get("consecutive_discards", 0) + 1
        status["iterations"] = max(status.get("iterations", 0), experiment.get("iteration", 0))
        status["memory_version"] = experiment_version if keep else previous_version
        self._write_status(agent_name, status)
        append_memory_outcome(
            agent_dir,
            experiment_version,
            "kept" if keep else "discarded",
            f"Evaluated over {len(eval_rounds)} rounds with win rate {new_wr:.1%} (delta {delta:+.1%}).",
            current_version=experiment_version if keep else previous_version,
        )
        self._append_results_row(
            agent_name=agent_name,
            iteration=experiment.get("iteration", status.get("iterations", 0)),
            strategy_version=strategy_version,
            win_rate=new_wr,
            delta=delta,
            rounds_played=len(eval_rounds),
            status="keep" if keep else "discard",
            description=experiment.get("description", "unknown"),
        )
        return {"status": "keep" if keep else "discard", "win_rate": new_wr, "delta": delta}

    async def evolve_agent(self, agent_name: str) -> dict | None:
        """
        Run the autoresearch inner loop for one agent:
        REVIEW → IDEATE → MODIFY (all in one Codex/GPT invocation).

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

        # Read agent memory and supplemental notes
        notes = read_memory_bundle(agent_dir)

        shared_knowledge_dir = os.path.join(self.data_dir, "shared_knowledge")
        if os.path.isdir(shared_knowledge_dir):
            ensure_shared_knowledge_forum(shared_knowledge_dir)
            notes += "\n\n" + build_shared_knowledge_context(shared_knowledge_dir, agent_name)

        # Build context
        shared_summary = _build_shared_ledger_summary(self.agents_dir, agent_name)
        lb_summary = _build_leaderboard_summary(self.data_dir)

        prompt = _build_evolution_prompt(
            agent_name, strategy, scripts, predictions,
            notes, shared_summary, lb_summary,
        )

        try:
            response = await run_codex_prompt(prompt, self.EVOLUTION_MODEL, agent_dir, self.timeout)
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
        status = self._read_status(agent_name)

        # Phase 4: SNAPSHOT — backup current strategy
        if os.path.exists(strategy_path):
            shutil.copy2(strategy_path, prev_path)
        self._snapshot_scripts(agent_dir)

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
        change_summary = result.get("change_summary", change)
        change_why = result.get("change_why", change)
        logger.info(f"Evolved {agent_name}: {change}")

        shared_dir = os.path.join(self.data_dir, "shared_knowledge")
        ensure_shared_knowledge_forum(shared_dir)
        forum = SharedKnowledgeForum(shared_dir)

        # Write shared discovery if present
        discovery = result.get("shared_discovery", "")
        if discovery and len(discovery) > 10:
            discovery_title = result.get("shared_discovery_title") or f"Discovery from {agent_name}"
            forum.create_post(agent_name, discovery_title, discovery)
            logger.info(f"  {agent_name} shared a discovery post to the shared knowledge forum")

        for vote in result.get("shared_votes", []):
            if not isinstance(vote, dict):
                continue
            forum.vote_post(
                agent_name,
                str(vote.get("post_id", "")),
                str(vote.get("vote", "")),
                str(vote.get("reason", "")),
            )

        for comment in result.get("shared_comments", []):
            if not isinstance(comment, dict):
                continue
            forum.comment_post(
                agent_name,
                str(comment.get("post_id", "")),
                str(comment.get("comment", "")),
            )

        scored = [p for p in read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
                  if p.get("correct") is not None]
        recent = scored[-self.evaluation_window:] if self.evaluation_window else scored
        wins = sum(1 for p in recent if p["correct"])
        baseline_wr = wins / len(recent) if recent else 0.0
        iteration = status.get("iterations", 0) + 1
        applied_at_round = scored[-1]["round"] if scored else -1
        previous_memory_version = status.get("memory_version", current_memory_version(agent_dir, "v1.0"))
        memory_version = next_memory_version(previous_memory_version)
        append_memory_entry(
            agent_dir,
            memory_version,
            change_summary,
            change_why,
            status="pending",
        )
        experiment = {
            "iteration": iteration,
            "description": change,
            "baseline_win_rate": baseline_wr,
            "baseline_rounds": len(recent),
            "applied_at_round": applied_at_round,
            "strategy_version": f"{hash(new_strategy) & 0xFFFFFFFF:08x}",
            "model": status.get("model", self.EVOLUTION_MODEL),
            "memory_version": memory_version,
            "previous_memory_version": previous_memory_version,
        }
        status["iterations"] = iteration
        status["last_action"] = "evolve_pending"
        status["last_action_round"] = applied_at_round
        status["current_experiment"] = experiment
        status["memory_version"] = memory_version
        self._write_status(agent_name, status)
        self._append_results_row(
            agent_name=agent_name,
            iteration=iteration,
            strategy_version=experiment["strategy_version"],
            win_rate=baseline_wr,
            delta=0.0,
            rounds_played=len(recent),
            status="pending",
            description=change,
        )

        return True
