import json
import os
import logging
import time

from src.codex_cli import DEFAULT_PREDICTION_MODEL, normalize_model_name
from src.io_utils import atomic_append_jsonl, atomic_write_json, read_jsonl
from src.memory_utils import read_memory_bundle
from src.runner.paper_execution import score_execution

logger = logging.getLogger(__name__)


def build_agent_context(agent_dir: str, snapshot: dict) -> str:
    parts = []
    strategy_path = os.path.join(agent_dir, "strategy.md")
    if os.path.exists(strategy_path):
        with open(strategy_path) as f:
            parts.append(f"## Strategy\n{f.read()}")
    memory = read_memory_bundle(agent_dir).strip()
    if memory:
        parts.append(f"## Memory\n{memory}")
    scripts_dir = os.path.join(agent_dir, "scripts")
    if os.path.isdir(scripts_dir):
        for fname in sorted(os.listdir(scripts_dir)):
            fpath = os.path.join(scripts_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath) as f:
                    parts.append(f"## Script: {fname}\n```\n{f.read()}\n```")
    results_path = os.path.join(agent_dir, "results.tsv")
    if os.path.exists(results_path):
        with open(results_path) as f:
            content = f.read().strip()
            if content:
                lines = content.split("\n")
                recent = "\n".join(lines[-20:])
                parts.append(f"## Recent Results\n{recent}")
    parts.append(f"## Market Snapshot\n```json\n{json.dumps(snapshot, indent=2)}\n```")
    return "\n\n".join(parts)


def score_prediction(prediction: str, outcome: str) -> bool:
    return prediction.strip().lower() == outcome.strip().lower()


class AgentRunner:
    def __init__(self, agents_dir: str, data_dir: str, prediction_deadline: int = 90, evaluation_window: int = 5):
        self.agents_dir = agents_dir
        self.data_dir = data_dir
        self.prediction_deadline = prediction_deadline
        self.evaluation_window = evaluation_window

    def discover_agents(self) -> list[str]:
        if not os.path.isdir(self.agents_dir):
            return []
        agents = []
        for name in sorted(os.listdir(self.agents_dir)):
            agent_dir = os.path.join(self.agents_dir, name)
            if os.path.isdir(agent_dir) and os.path.exists(os.path.join(agent_dir, "strategy.md")):
                agents.append(name)
        return agents

    def _status_path(self, agent_name: str) -> str:
        return os.path.join(self.agents_dir, agent_name, "status.json")

    def _executions_path(self, agent_name: str) -> str:
        return os.path.join(self.agents_dir, agent_name, "executions.jsonl")

    def _shared_prediction_path(self, agent_name: str, round_timestamp: int) -> str:
        return os.path.join(
            self.data_dir,
            "rounds",
            str(round_timestamp),
            "predictions",
            f"{agent_name}.json",
        )

    def _shared_prediction_updates_path(self, agent_name: str, round_timestamp: int) -> str:
        return os.path.join(
            self.data_dir,
            "rounds",
            str(round_timestamp),
            "prediction-updates",
            f"{agent_name}.jsonl",
        )

    def _infer_archetype(self, agent_name: str) -> str:
        suffix = agent_name.split("-", 2)[-1] if "-" in agent_name else agent_name
        while True:
            changed = False
            for prefix in ("clone-", "mirror-"):
                if suffix.startswith(prefix):
                    suffix = suffix[len(prefix):]
                    changed = True
            if not changed:
                break
        return suffix

    def _read_status(self, agent_name: str) -> dict:
        status_path = self._status_path(agent_name)
        if not os.path.exists(status_path):
            return {}
        try:
            with open(status_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _build_status(self, agent_name: str, previous: dict | None = None) -> dict:
        previous = previous or {}
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        preds = read_jsonl(agent_pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        wins = sum(1 for p in scored if p["correct"])
        ew = scored[-self.evaluation_window:] if self.evaluation_window else scored
        ew_wins = sum(1 for p in ew if p.get("correct")) if ew else 0
        latest = preds[-1] if preds else {}
        agent_config = {}
        config_path = os.path.join(self.agents_dir, agent_name, "agent_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    agent_config = json.load(f)
            except (json.JSONDecodeError, OSError):
                agent_config = {}

        status = {
            "agent_id": agent_name,
            "archetype": self._infer_archetype(agent_name),
            "total_rounds": len(scored),
            "total_correct": wins,
            "all_time_win_rate": wins / len(scored) if scored else 0.0,
            "ew_win_rate": ew_wins / len(ew) if ew else 0.0,
            "model": normalize_model_name(agent_config.get("model"), DEFAULT_PREDICTION_MODEL),
            "mirror": agent_config.get("mirror", False),
            "source_agent": agent_config.get("source_agent"),
            "last_action": previous.get("last_action", "spawn"),
            "last_action_round": previous.get("last_action_round"),
            "iterations": previous.get("iterations", 0),
            "consecutive_discards": previous.get("consecutive_discards", 0),
            "current_experiment": previous.get("current_experiment"),
            "prediction_deadline_seconds": self.prediction_deadline,
            "last_prediction_round": latest.get("round"),
            "last_prediction": latest.get("prediction"),
            "last_outcome": latest.get("outcome"),
            "last_correct": latest.get("correct"),
            "last_strategy_version": latest.get("strategy_version"),
            "status": previous.get("status", "active"),
            "memory_version": previous.get("memory_version", "v1.0"),
            "updated_at": int(time.time() * 1000),
        }
        return status

    def _write_status(self, agent_name: str, last_action: str | None = None, last_action_round: int | None = None):
        previous = self._read_status(agent_name)
        status = self._build_status(agent_name, previous)
        if last_action is not None:
            status["last_action"] = last_action
        if last_action_round is not None:
            status["last_action_round"] = last_action_round
        atomic_write_json(self._status_path(agent_name), status)
        return status

    def _write_predictions_file(self, path: str, records: list[dict]) -> None:
        tmp_path = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w") as f:
            for record in records:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.rename(tmp_path, path)

    def refresh_shared_ledger(self) -> dict:
        agents = []
        for agent_name in self.discover_agents():
            status = self._read_status(agent_name)
            if not status:
                status = self._write_status(agent_name)
            agents.append(status)

        agents.sort(
            key=lambda s: (
                s.get("ew_win_rate", 0.0),
                s.get("all_time_win_rate", 0.0),
                s.get("total_rounds", 0),
            ),
            reverse=True,
        )
        ledger = {
            "updated_at": int(time.time() * 1000),
            "agents": agents,
        }
        atomic_write_json(os.path.join(self.data_dir, "live", "shared-ledger.json"), ledger)
        return ledger

    def record_prediction(
        self,
        agent_name: str,
        round_timestamp: int,
        prediction: str,
        confidence: float,
        reasoning: str,
        strategy_version: str,
        execution_quote: dict | None = None,
    ):
        predicted_at = int(time.time() * 1000)
        record = {
            "round": round_timestamp,
            "agent": agent_name,
            "prediction": prediction,
            "confidence": confidence,
            "reasoning": reasoning,
            "strategy_version": strategy_version,
            "predicted_at": predicted_at,
            "outcome": None,
            "correct": None,
            "active": True,
        }
        if execution_quote:
            record.update(execution_quote)
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        preds = read_jsonl(agent_pred_path)
        revision = 1
        for pred in preds:
            if pred.get("round") != round_timestamp:
                continue
            revision = max(revision, int(pred.get("revision", 1)) + 1)
            if pred.get("outcome") is None and pred.get("active", True):
                pred["active"] = False
                pred["superseded_at"] = predicted_at
        record["revision"] = revision
        preds.append(record)
        self._write_predictions_file(agent_pred_path, preds)

        shared_record = {
            "round_id": round_timestamp,
            "agent_id": agent_name,
            "prediction": prediction,
            "confidence": confidence,
            "reasoning": reasoning,
            "strategy_version": strategy_version,
            "predicted_at": predicted_at,
            "outcome": None,
            "correct": None,
            "revision": revision,
        }
        if execution_quote:
            shared_record.update(execution_quote)
        atomic_write_json(
            self._shared_prediction_path(agent_name, round_timestamp),
            shared_record,
        )
        atomic_append_jsonl(
            self._shared_prediction_updates_path(agent_name, round_timestamp),
            shared_record,
        )
        self._write_status(agent_name, last_action="predict", last_action_round=round_timestamp)
        self.refresh_shared_ledger()
        return record

    def score_round(self, agent_name: str, round_timestamp: int, outcome: str):
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        preds = read_jsonl(agent_pred_path)
        scored_record = None
        for pred in reversed(preds):
            if (
                pred["round"] == round_timestamp
                and pred.get("outcome") is None
                and pred.get("active", True)
            ):
                pred["outcome"] = outcome
                pred["correct"] = score_prediction(pred["prediction"], outcome)
                scored_record = pred
                break
        if scored_record is None:
            for pred in reversed(preds):
                if pred["round"] == round_timestamp and pred.get("outcome") is None:
                    pred["outcome"] = outcome
                    pred["correct"] = score_prediction(pred["prediction"], outcome)
                    pred["active"] = pred.get("active", True)
                    scored_record = pred
                    break
        self._write_predictions_file(agent_pred_path, preds)
        if scored_record is not None:
            shared_path = self._shared_prediction_path(agent_name, round_timestamp)
            if os.path.exists(shared_path):
                atomic_write_json(
                    shared_path,
                    {
                        "round_id": round_timestamp,
                        "agent_id": agent_name,
                        "prediction": scored_record["prediction"],
                        "confidence": scored_record.get("confidence", 0.5),
                        "reasoning": scored_record.get("reasoning", ""),
                        "strategy_version": scored_record.get("strategy_version"),
                        "predicted_at": scored_record.get("predicted_at"),
                        "outcome": outcome,
                        "correct": scored_record["correct"],
                        "revision": scored_record.get("revision", 1),
                        "entry_price": scored_record.get("entry_price"),
                        "asset_id": scored_record.get("asset_id"),
                        "market_id": scored_record.get("market_id"),
                        "market_slug": scored_record.get("market_slug"),
                    },
                )
            execution = score_execution(scored_record, outcome)
            if execution is not None:
                exec_record = {
                    "round": round_timestamp,
                    "agent": agent_name,
                    "prediction": scored_record["prediction"],
                    "correct": scored_record["correct"],
                    "market_id": scored_record.get("market_id"),
                    "market_slug": scored_record.get("market_slug"),
                    "asset_id": scored_record.get("asset_id"),
                    "predicted_at": scored_record.get("predicted_at"),
                    "revision": scored_record.get("revision", 1),
                    **execution,
                }
                atomic_append_jsonl(self._executions_path(agent_name), exec_record)
                atomic_append_jsonl(
                    os.path.join(self.data_dir, "history", "executions.jsonl"),
                    exec_record,
                )
        self._write_status(agent_name, last_action="score", last_action_round=round_timestamp)
        self.refresh_shared_ledger()

    def get_agent_win_rate(self, agent_name: str, window: int | None = None) -> tuple[float, int]:
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        preds = read_jsonl(agent_pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        if window:
            scored = scored[-window:]
        if not scored:
            return 0.0, 0
        correct = sum(1 for p in scored if p["correct"])
        return correct / len(scored), len(scored)

    def get_losing_streak(self, agent_name: str) -> int:
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        preds = read_jsonl(agent_pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        streak = 0
        for p in reversed(scored):
            if not p["correct"]:
                streak += 1
            else:
                break
        return streak
