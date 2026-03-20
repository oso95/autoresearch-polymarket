# src/data_layer/round_manager.py
import json
import os
import time
import logging

from src.io_utils import atomic_write_json, atomic_append_jsonl

logger = logging.getLogger(__name__)

class RoundManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._current_round: int | None = None

    def freeze_snapshot(self, timestamp: int) -> str:
        round_dir = os.path.join(self.data_dir, "rounds", str(timestamp))
        os.makedirs(round_dir, exist_ok=True)
        os.makedirs(os.path.join(round_dir, "predictions"), exist_ok=True)
        os.makedirs(os.path.join(round_dir, "prediction-updates"), exist_ok=True)
        snapshot = {}
        live_dir = os.path.join(self.data_dir, "live")
        for fname in os.listdir(live_dir):
            if fname.endswith(".json") and fname != "status.json" and fname != "heartbeat.json":
                key = fname.replace(".json", "")
                with open(os.path.join(live_dir, fname)) as f:
                    snapshot[key] = json.load(f)
        polling = {}
        polling_dir = os.path.join(self.data_dir, "polling")
        if os.path.exists(polling_dir):
            for fname in os.listdir(polling_dir):
                if fname.endswith(".json"):
                    key = fname.replace(".json", "")
                    with open(os.path.join(polling_dir, fname)) as f:
                        polling[key] = json.load(f)
        snapshot["polling"] = polling
        snapshot["frozen_at"] = int(time.time() * 1000)
        snapshot["round_timestamp"] = timestamp
        snapshot_path = os.path.join(round_dir, "snapshot.json")
        atomic_write_json(snapshot_path, snapshot)
        atomic_write_json(
            os.path.join(self.data_dir, "live", "current-round.json"),
            {
                "round_timestamp": timestamp,
                "snapshot_path": snapshot_path,
                "frozen_at": snapshot["frozen_at"],
            },
        )
        self._current_round = timestamp
        logger.info(f"Froze snapshot for round {timestamp}")
        return snapshot_path

    def record_resolution(self, timestamp: int, outcome: str, open_price: float, close_price: float):
        round_dir = os.path.join(self.data_dir, "rounds", str(timestamp))
        result = {
            "round_timestamp": timestamp,
            "outcome": outcome,
            "open_price": open_price,
            "close_price": close_price,
            "resolved_at": int(time.time() * 1000),
        }
        result_path = os.path.join(round_dir, "result.json")
        atomic_write_json(result_path, result)
        history_path = os.path.join(self.data_dir, "history", "resolutions.jsonl")
        atomic_append_jsonl(history_path, result)
        logger.info(f"Recorded resolution for round {timestamp}: {outcome}")

    @property
    def current_round(self) -> int | None:
        return self._current_round
