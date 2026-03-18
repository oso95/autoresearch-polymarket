import json
import pytest
from src.coordinator.leaderboard import (
    compute_streak_adjusted_win_rate,
    build_leaderboard,
    LeaderboardEntry,
)

def test_streak_adjusted_no_penalty():
    assert compute_streak_adjusted_win_rate(0.6, 2) == 0.6

def test_streak_adjusted_with_penalty():
    result = compute_streak_adjusted_win_rate(0.6, 5)
    assert abs(result - 0.54) < 0.001

def test_streak_adjusted_floor():
    result = compute_streak_adjusted_win_rate(0.6, 100)
    assert result == 0.0

def test_build_leaderboard():
    agents = {
        "agent-001": {"win_rate": 0.65, "total_rounds": 100, "losing_streak": 0},
        "agent-002": {"win_rate": 0.45, "total_rounds": 60, "losing_streak": 5},
        "agent-003": {"win_rate": 0.55, "total_rounds": 30, "losing_streak": 1},
    }
    board = build_leaderboard(agents)
    assert board[0].agent_name == "agent-001"
    assert board[0].proven is True
    assert board[1].agent_name == "agent-003"
    assert board[2].agent_name == "agent-002"
