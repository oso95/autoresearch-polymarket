import json
import os
import time
import logging

logger = logging.getLogger(__name__)

class HealthMonitor:
    def __init__(self, data_dir: str, stale_threshold_seconds: int = 30):
        self.data_dir = data_dir
        self.stale_threshold = stale_threshold_seconds

    def is_data_layer_healthy(self) -> bool:
        hb_path = os.path.join(self.data_dir, "live", "heartbeat.json")
        if not os.path.exists(hb_path):
            return False
        try:
            with open(hb_path) as f:
                hb = json.load(f)
            age_seconds = (time.time() * 1000 - hb["timestamp"]) / 1000
            return age_seconds < self.stale_threshold
        except (json.JSONDecodeError, KeyError):
            return False

    def is_round_data_stale(self) -> bool:
        status_path = os.path.join(self.data_dir, "live", "status.json")
        if not os.path.exists(status_path):
            return True
        try:
            with open(status_path) as f:
                status = json.load(f)
            return status.get("stale", True)
        except (json.JSONDecodeError, KeyError):
            return True

    def is_ready(self) -> bool:
        status_path = os.path.join(self.data_dir, "live", "status.json")
        if not os.path.exists(status_path):
            return False
        try:
            with open(status_path) as f:
                status = json.load(f)
            return status.get("ready", False) and not status.get("stale", True)
        except (json.JSONDecodeError, KeyError):
            return False

    def wait_for_ready(self, timeout: int = 60, poll_interval: float = 1.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.is_ready():
                return True
            time.sleep(poll_interval)
        return False
