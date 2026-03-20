# src/coordinator/tournament.py
import json
import os
import time
import logging
from dataclasses import asdict

from src.codex_cli import DEFAULT_PREDICTION_MODEL, normalize_model_name
from src.config import Config
from src.coordinator.leaderboard import build_leaderboard
from src.coordinator.spawner import AgentSpawner, SEED_STRATEGIES
from src.runner.agent_runner import AgentRunner
from src.io_utils import atomic_write_json, atomic_append_jsonl

logger = logging.getLogger(__name__)

INITIAL_SCREENING_KILL_RATE = 0.30
MODEL_EXPERIMENT_MODELS = ()

class Tournament:
    def __init__(self, config: Config, spawner: AgentSpawner, runner: AgentRunner, data_dir: str):
        self.config = config
        self.spawner = spawner
        self.runner = runner
        self.data_dir = data_dir
        self._graveyard_dir = os.path.join(data_dir, "coordinator", "graveyard")

    def _load_agent_status(self, agent_name: str) -> dict:
        return self.runner._write_status(agent_name)

    def _infer_agent_stats(self, agent_name: str) -> dict:
        status = self._load_agent_status(agent_name)
        streak = self.runner.get_losing_streak(agent_name)
        return {
            "win_rate": status.get("all_time_win_rate", 0.0),
            "all_time_win_rate": status.get("all_time_win_rate", 0.0),
            "ew_win_rate": status.get("ew_win_rate", status.get("all_time_win_rate", 0.0)),
            "total_rounds": status.get("total_rounds", 0),
            "losing_streak": streak,
            "model": normalize_model_name(status.get("model"), DEFAULT_PREDICTION_MODEL),
            "status": status.get("status", "active"),
            "current_experiment": status.get("current_experiment"),
        }

    def _model_variant_exists(self, source_agent: str, model: str, alive: list[str]) -> bool:
        for name in alive:
            status = self._load_agent_status(name)
            if status.get("source_agent") == source_agent and normalize_model_name(status.get("model"), DEFAULT_PREDICTION_MODEL) == model:
                return True
            if name == source_agent and normalize_model_name(status.get("model"), DEFAULT_PREDICTION_MODEL) == model:
                return True
        return False

    def should_kill(self, agent_name: str) -> bool:
        status = self._infer_agent_stats(agent_name)
        win_rate = status["all_time_win_rate"]
        total = status["total_rounds"]
        score = (status["ew_win_rate"] * 0.6) + (status["all_time_win_rate"] * 0.4)
        if total > self.config.initial_screening_rounds and win_rate < INITIAL_SCREENING_KILL_RATE:
            return True
        if total >= self.config.kill_min_rounds and score < self.config.kill_threshold_win_rate:
            return True
        return False

    def run_cycle(self) -> dict:
        agents = self.runner.discover_agents()
        if not agents:
            return {"action": "none", "reason": "no agents"}

        agent_stats = {}
        for name in agents:
            agent_stats[name] = self._infer_agent_stats(name)

        board = build_leaderboard(agent_stats)
        leaderboard_path = os.path.join(self.data_dir, "coordinator", "leaderboard.json")
        atomic_write_json(leaderboard_path, {"entries": [asdict(e) for e in board], "updated_at": int(time.time() * 1000)})

        actions = []

        alive = list(agents)
        for entry in reversed(board):
            if len(alive) <= self.config.min_agents:
                break
            if self.should_kill(entry.agent_name):
                self.spawner.retire_agent(entry.agent_name, self._graveyard_dir)
                alive.remove(entry.agent_name)
                actions.append({"type": "kill", "agent": entry.agent_name, "win_rate": entry.win_rate})

        seed_idx = 0
        while len(alive) < self.config.min_agents and seed_idx < len(SEED_STRATEGIES):
            name = self.spawner.spawn_from_seed(SEED_STRATEGIES[seed_idx])
            alive.append(name)
            actions.append({"type": "spawn", "agent": name, "seed": SEED_STRATEGIES[seed_idx]["name"]})
            seed_idx += 1

        # Clone top 2-3 agents with diverse mutations (prefer different strategy families)
        cloned_bases = set()
        mutation_ideas = [
            "Try a more aggressive threshold (lower confidence requirement)",
            "Add a contrarian filter: if signal agrees with market consensus, reduce confidence",
            "Incorporate time-of-day weighting (different hours have different patterns)",
            "Add a volatility regime check: only trade when ATR is in a specific range",
            "Experiment with inverting your weakest signal source",
            "Try combining your approach with Fibonacci time zones",
        ]
        clone_count = 0
        for i, entry in enumerate(board):
            if len(alive) >= self.config.max_agents or clone_count >= 3:
                break
            if entry.total_rounds < 10:
                continue
            if entry.agent_name not in alive:
                continue
            # Prefer diversity: extract strategy family from agent name
            # agent-NNN-<type> → strip clone-/mirror- prefixes to get family
            parts = entry.agent_name.split("-", 2)
            suffix = parts[2] if len(parts) > 2 else ""
            for prefix in ("clone-", "mirror-"):
                while suffix.startswith(prefix):
                    suffix = suffix[len(prefix):]
            base = suffix or entry.agent_name
            if base in cloned_bases:
                continue
            cloned_bases.add(base)
            mutation = mutation_ideas[clone_count % len(mutation_ideas)]
            clone_name = self.spawner.clone_agent(entry.agent_name, mutation)
            alive.append(clone_name)
            actions.append({"type": "clone", "source": entry.agent_name, "clone": clone_name})
            clone_count += 1

        # Model experiments: run top strategies on stronger models in parallel.
        model_variant_count = 0
        for entry in board:
            if len(alive) >= self.config.max_agents or model_variant_count >= 2:
                break
            if entry.total_rounds < 10:
                continue
            for candidate_model in MODEL_EXPERIMENT_MODELS:
                if candidate_model == entry.model:
                    continue
                if self._model_variant_exists(entry.agent_name, candidate_model, alive):
                    continue
                mutation = f"Model experiment: run the same strategy on {candidate_model} for parallel comparison"
                variant_name = self.spawner.clone_agent(
                    entry.agent_name,
                    mutation,
                    agent_config={"model": candidate_model, "source_agent": entry.agent_name},
                )
                alive.append(variant_name)
                actions.append({
                    "type": "model-variant",
                    "source": entry.agent_name,
                    "variant": variant_name,
                    "model": candidate_model,
                })
                model_variant_count += 1
                break

        # Auto-mirror: if an agent is extremely anti-predictive (below 35% with 20+ rounds),
        # spawn a mirror that inverts its signal — these are the most valuable mirror candidates
        MIRROR_THRESHOLD = 0.40  # Mirror any agent below 40% (inverted = 60%+)
        MIRROR_MIN_ROUNDS = 15
        for entry in reversed(board):
            if len(alive) >= self.config.max_agents:
                break
            if entry.agent_name not in alive:
                continue
            if entry.total_rounds < MIRROR_MIN_ROUNDS:
                continue
            if entry.win_rate >= MIRROR_THRESHOLD:
                continue
            # Don't mirror a mirror
            if "mirror" in entry.agent_name:
                continue
            # Check if mirror already exists
            existing_mirrors = [a for a in alive if f"mirror-{entry.agent_name.split('-', 2)[-1]}" in a]
            if existing_mirrors:
                continue
            mirror_name = self.spawner.spawn_mirror(entry.agent_name)
            alive.append(mirror_name)
            expected_wr = 1.0 - entry.win_rate
            actions.append({
                "type": "auto-mirror",
                "source": entry.agent_name,
                "mirror": mirror_name,
                "source_win_rate": entry.win_rate,
                "expected_win_rate": expected_wr,
            })
            logger.info(
                f"  Auto-mirror: {entry.agent_name} ({entry.win_rate:.1%}) → "
                f"{mirror_name} (expected ~{expected_wr:.1%})"
            )

        alerts_path = os.path.join(self.data_dir, "coordinator", "alerts.jsonl")
        for entry in board:
            if entry.proven:
                atomic_append_jsonl(alerts_path, {
                    "type": "proven_strategy", "agent": entry.agent_name,
                    "win_rate": entry.win_rate, "total_rounds": entry.total_rounds,
                    "timestamp": int(time.time() * 1000),
                })

        log_path = os.path.join(self.data_dir, "coordinator", "tournament_log.tsv")
        for action in actions:
            line = f"{int(time.time())}\t{action['type']}\t{json.dumps(action)}\n"
            with open(log_path, "a") as f:
                f.write(line)

        return {"actions": actions, "leaderboard": [asdict(e) for e in board]}

    def spawn_initial_agents(self):
        for seed in SEED_STRATEGIES[:self.config.min_agents]:
            self.spawner.spawn_from_seed(seed)
        logger.info(f"Spawned {self.config.min_agents} initial seed agents")
