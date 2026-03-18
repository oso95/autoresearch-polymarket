import os
import shutil
import logging

from src.io_utils import read_jsonl

logger = logging.getLogger(__name__)

class FastFailChecker:
    def __init__(self, streak_threshold: int = 3):
        self.streak_threshold = streak_threshold

    def should_revert(self, agent_dir: str) -> bool:
        pred_path = os.path.join(agent_dir, "predictions.jsonl")
        preds = read_jsonl(pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        if len(scored) < self.streak_threshold:
            return False
        recent = scored[-self.streak_threshold:]
        return all(not p["correct"] for p in recent)

    def revert_strategy(self, agent_dir: str):
        prev_path = os.path.join(agent_dir, "strategy.md.prev")
        current_path = os.path.join(agent_dir, "strategy.md")
        if not os.path.exists(prev_path):
            logger.warning(f"No strategy.md.prev in {agent_dir}, cannot revert")
            return
        shutil.copy2(prev_path, current_path)
        logger.info(f"Reverted strategy in {agent_dir} to previous version")
