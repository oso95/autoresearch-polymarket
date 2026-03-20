from dataclasses import dataclass

from src.codex_cli import DEFAULT_PREDICTION_MODEL, normalize_model_name

PROVEN_WIN_RATE = 0.55
PROVEN_MIN_ROUNDS = 100

def compute_streak_adjusted_win_rate(win_rate: float, losing_streak: int) -> float:
    penalty = 0.05 * max(0, losing_streak - 3)
    adjusted = win_rate * (1 - penalty)
    return max(0.0, adjusted)

@dataclass
class LeaderboardEntry:
    agent_name: str
    win_rate: float
    ew_win_rate: float
    all_time_win_rate: float
    agent_score: float
    streak_adjusted: float
    total_rounds: int
    losing_streak: int
    proven: bool
    model: str = DEFAULT_PREDICTION_MODEL
    status: str = "active"

def build_leaderboard(agents: dict[str, dict]) -> list[LeaderboardEntry]:
    entries = []
    for name, stats in agents.items():
        all_time = stats.get("all_time_win_rate", stats["win_rate"])
        ew = stats.get("ew_win_rate", all_time)
        wr = all_time
        streak = stats["losing_streak"]
        score = (ew * 0.6) + (all_time * 0.4)
        adjusted = compute_streak_adjusted_win_rate(score, streak)
        proven = ew >= PROVEN_WIN_RATE and stats["total_rounds"] >= PROVEN_MIN_ROUNDS
        entries.append(LeaderboardEntry(
            agent_name=name,
            win_rate=wr,
            ew_win_rate=ew,
            all_time_win_rate=all_time,
            agent_score=score,
            streak_adjusted=adjusted,
            total_rounds=stats["total_rounds"],
            losing_streak=streak,
            proven=proven,
            model=normalize_model_name(stats.get("model"), DEFAULT_PREDICTION_MODEL),
            status=stats.get("status", "active"),
        ))
    entries.sort(key=lambda e: e.streak_adjusted, reverse=True)
    return entries
