# src/config.py
import json
from dataclasses import dataclass, field

@dataclass
class Config:
    min_agents: int = 3
    max_agents: int = 10
    evaluation_window_rounds: int = 5
    fast_fail_streak: int = 3
    coordinator_frequency_rounds: int = 20
    kill_threshold_win_rate: float = 0.45
    kill_min_rounds: int = 50
    prediction_deadline_seconds: int = 90
    initial_screening_rounds: int = 15
    data_dir: str = "data"
    agents_dir: str = "agents"

def load_config(path: str) -> Config:
    with open(path) as f:
        data = json.load(f)
    return Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
