from dataclasses import dataclass

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
    streak_adjusted: float
    total_rounds: int
    losing_streak: int
    proven: bool

def build_leaderboard(agents: dict[str, dict]) -> list[LeaderboardEntry]:
    entries = []
    for name, stats in agents.items():
        wr = stats["win_rate"]
        streak = stats["losing_streak"]
        adjusted = compute_streak_adjusted_win_rate(wr, streak)
        proven = wr >= PROVEN_WIN_RATE and stats["total_rounds"] >= PROVEN_MIN_ROUNDS
        entries.append(LeaderboardEntry(
            agent_name=name, win_rate=wr, streak_adjusted=adjusted,
            total_rounds=stats["total_rounds"], losing_streak=streak, proven=proven,
        ))
    entries.sort(key=lambda e: e.streak_adjusted, reverse=True)
    return entries
