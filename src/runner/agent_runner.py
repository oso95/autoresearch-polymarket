import json
import os
import logging
import time

from src.io_utils import atomic_append_jsonl, read_jsonl

logger = logging.getLogger(__name__)


def build_agent_context(agent_dir: str, snapshot: dict) -> str:
    parts = []
    strategy_path = os.path.join(agent_dir, "strategy.md")
    if os.path.exists(strategy_path):
        with open(strategy_path) as f:
            parts.append(f"## Strategy\n{f.read()}")
    notes_path = os.path.join(agent_dir, "notes.md")
    if os.path.exists(notes_path):
        with open(notes_path) as f:
            content = f.read().strip()
            if content:
                parts.append(f"## Notes\n{content}")
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
    def __init__(self, agents_dir: str, data_dir: str, prediction_deadline: int = 90):
        self.agents_dir = agents_dir
        self.data_dir = data_dir
        self.prediction_deadline = prediction_deadline

    def discover_agents(self) -> list[str]:
        if not os.path.isdir(self.agents_dir):
            return []
        agents = []
        for name in sorted(os.listdir(self.agents_dir)):
            agent_dir = os.path.join(self.agents_dir, name)
            if os.path.isdir(agent_dir) and os.path.exists(os.path.join(agent_dir, "strategy.md")):
                agents.append(name)
        return agents

    def record_prediction(self, agent_name: str, round_timestamp: int, prediction: str, confidence: float, reasoning: str, strategy_version: str):
        record = {
            "round": round_timestamp,
            "agent": agent_name,
            "prediction": prediction,
            "confidence": confidence,
            "reasoning": reasoning,
            "strategy_version": strategy_version,
            "predicted_at": int(time.time() * 1000),
            "outcome": None,
            "correct": None,
        }
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        atomic_append_jsonl(agent_pred_path, record)
        return record

    def score_round(self, agent_name: str, round_timestamp: int, outcome: str):
        agent_pred_path = os.path.join(self.agents_dir, agent_name, "predictions.jsonl")
        preds = read_jsonl(agent_pred_path)
        for pred in preds:
            if pred["round"] == round_timestamp and pred["outcome"] is None:
                pred["outcome"] = outcome
                pred["correct"] = score_prediction(pred["prediction"], outcome)
                break
        tmp_path = agent_pred_path + ".tmp"
        with open(tmp_path, "w") as f:
            for p in preds:
                f.write(json.dumps(p, separators=(",", ":")) + "\n")
        os.rename(tmp_path, agent_pred_path)

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
