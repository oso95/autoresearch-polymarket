"""Tests for outcome_analyzer module."""
import json
import os
import tempfile

import pytest

from src.runner.outcome_analyzer import (
    analyze_patterns,
    analyze_time_patterns,
    build_outcome_context,
    compute_autocorrelation,
    get_pattern_signal,
    load_outcome_sequence,
    run_full_analysis,
    _compute_streaks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rounds_dir(tmpdir: str, rounds: list[tuple[int, str]]):
    """Create data/rounds/{ts}/result.json files."""
    for ts, outcome in rounds:
        rdir = os.path.join(tmpdir, "data", "rounds", str(ts))
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, "result.json"), "w") as f:
            json.dump({"outcome": outcome, "round_timestamp": ts}, f)
    return os.path.join(tmpdir, "data")


# ---------------------------------------------------------------------------
# compute_autocorrelation
# ---------------------------------------------------------------------------

class TestAutocorrelation:
    def test_all_up(self):
        """All same outcome -> autocorrelation = +1 at all lags."""
        ac = compute_autocorrelation(["Up"] * 20, max_lag=5)
        for lag in range(1, 6):
            assert ac[lag] == 1.0, f"lag {lag} should be 1.0 for all-same"

    def test_all_down(self):
        """All same outcome -> autocorrelation = +1 at all lags."""
        ac = compute_autocorrelation(["Down"] * 20, max_lag=5)
        for lag in range(1, 6):
            assert ac[lag] == 1.0

    def test_alternating(self):
        """Perfect alternation -> lag-1 autocorrelation should be negative."""
        seq = ["Up", "Down"] * 50
        ac = compute_autocorrelation(seq, max_lag=3)
        assert ac[1] < -0.9, f"Expected strongly negative lag-1, got {ac[1]}"
        # lag-2 should be positive (Up->Up, Down->Down at distance 2)
        assert ac[2] > 0.9, f"Expected strongly positive lag-2, got {ac[2]}"

    def test_empty(self):
        ac = compute_autocorrelation([], max_lag=5)
        assert ac == {}

    def test_single_element(self):
        ac = compute_autocorrelation(["Up"], max_lag=5)
        assert ac == {}

    def test_two_elements_same(self):
        ac = compute_autocorrelation(["Up", "Up"], max_lag=5)
        assert 1 in ac
        assert ac[1] == 1.0

    def test_two_elements_different(self):
        ac = compute_autocorrelation(["Up", "Down"], max_lag=5)
        assert 1 in ac
        # With 2 elements, one Up one Down: mean=0, values=[1,-1]
        # cov at lag 1: (1-0)*(-1-0)/1 = -1, var = 1 -> ac = -1
        assert ac[1] == -1.0

    def test_max_lag_respected(self):
        ac = compute_autocorrelation(["Up", "Down"] * 10, max_lag=3)
        assert max(ac.keys()) == 3


# ---------------------------------------------------------------------------
# analyze_patterns
# ---------------------------------------------------------------------------

class TestPatternAnalysis:
    def test_simple_pattern(self):
        # After "Up-Up", what happens?
        seq = ["Up", "Up", "Down", "Up", "Up", "Up", "Up", "Up", "Down"]
        stats = analyze_patterns(seq, pattern_lengths=[2])
        assert "Up-Up" in stats
        uu = stats["Up-Up"]
        assert uu["total"] == uu["next_up"] + uu["next_down"]
        assert uu["total"] > 0

    def test_trigram(self):
        seq = ["Up", "Down", "Up", "Down", "Up", "Down", "Up"]
        stats = analyze_patterns(seq, pattern_lengths=[3])
        assert "Up-Down-Up" in stats
        udu = stats["Up-Down-Up"]
        assert udu["total"] > 0

    def test_probabilities_sum_to_one(self):
        seq = ["Up", "Down", "Up", "Down", "Up", "Up", "Down", "Down", "Up"]
        stats = analyze_patterns(seq, pattern_lengths=[2])
        for key, entry in stats.items():
            assert abs(entry["p_up"] + entry["p_down"] - 1.0) < 0.001, f"Probabilities for {key} don't sum to 1"

    def test_empty_sequence(self):
        stats = analyze_patterns([], pattern_lengths=[2])
        assert stats == {}

    def test_too_short_for_pattern(self):
        stats = analyze_patterns(["Up", "Down"], pattern_lengths=[2])
        assert stats == {}

    def test_default_pattern_lengths(self):
        seq = ["Up", "Down", "Up", "Down", "Up", "Down", "Up", "Down"]
        stats = analyze_patterns(seq)
        # Should have both 2-grams and 3-grams
        has_bigram = any(k.count("-") == 1 for k in stats)
        has_trigram = any(k.count("-") == 2 for k in stats)
        assert has_bigram
        assert has_trigram


# ---------------------------------------------------------------------------
# analyze_time_patterns
# ---------------------------------------------------------------------------

class TestTimePatterns:
    def test_hour_grouping(self):
        sequence = [
            {"timestamp": 1000, "outcome": "Up", "hour_utc": 10, "weekday": "Monday"},
            {"timestamp": 1300, "outcome": "Up", "hour_utc": 10, "weekday": "Monday"},
            {"timestamp": 1600, "outcome": "Down", "hour_utc": 10, "weekday": "Monday"},
            {"timestamp": 1900, "outcome": "Down", "hour_utc": 14, "weekday": "Monday"},
        ]
        result = analyze_time_patterns(sequence)
        assert 10 in result["hourly"]
        assert result["hourly"][10]["up_rate"] == pytest.approx(2 / 3, abs=0.01)
        assert result["hourly"][10]["total"] == 3
        assert 14 in result["hourly"]
        assert result["hourly"][14]["up_rate"] == 0.0

    def test_weekday_grouping(self):
        sequence = [
            {"timestamp": 1000, "outcome": "Up", "hour_utc": 10, "weekday": "Monday"},
            {"timestamp": 1300, "outcome": "Down", "hour_utc": 11, "weekday": "Tuesday"},
            {"timestamp": 1600, "outcome": "Up", "hour_utc": 12, "weekday": "Monday"},
        ]
        result = analyze_time_patterns(sequence)
        assert "Monday" in result["daily"]
        assert result["daily"]["Monday"]["up_rate"] == 1.0
        assert result["daily"]["Tuesday"]["up_rate"] == 0.0

    def test_empty_sequence(self):
        result = analyze_time_patterns([])
        assert result["hourly"] == {}
        assert result["daily"] == {}


# ---------------------------------------------------------------------------
# get_pattern_signal
# ---------------------------------------------------------------------------

class TestPatternSignal:
    def test_clear_signal(self):
        # Build pattern stats where "Up-Up" strongly predicts Down
        pattern_stats = {
            "Up-Up": {"next_up": 2, "next_down": 8, "total": 10, "p_up": 0.2, "p_down": 0.8},
        }
        signal = get_pattern_signal(["Up", "Up"], pattern_stats)
        assert signal["direction"] == "Down"
        assert signal["confidence"] > 0.5
        assert signal["pattern"] == "Up-Up"
        assert signal["sample_size"] == 10

    def test_trigram_preferred_over_bigram(self):
        pattern_stats = {
            "Up-Down-Up": {"next_up": 1, "next_down": 9, "total": 10, "p_up": 0.1, "p_down": 0.9},
            "Down-Up": {"next_up": 7, "next_down": 3, "total": 10, "p_up": 0.7, "p_down": 0.3},
        }
        signal = get_pattern_signal(["Up", "Down", "Up"], pattern_stats)
        assert signal["pattern"] == "Up-Down-Up"
        assert signal["direction"] == "Down"

    def test_fallback_to_bigram(self):
        pattern_stats = {
            "Down-Up": {"next_up": 8, "next_down": 2, "total": 10, "p_up": 0.8, "p_down": 0.2},
        }
        signal = get_pattern_signal(["Down", "Up"], pattern_stats)
        assert signal["direction"] == "Up"
        assert signal["pattern"] == "Down-Up"

    def test_empty_outcomes(self):
        signal = get_pattern_signal([], {"Up-Up": {"next_up": 5, "next_down": 5, "total": 10, "p_up": 0.5, "p_down": 0.5}})
        assert signal["direction"] == "Neutral"

    def test_empty_stats(self):
        signal = get_pattern_signal(["Up", "Up"], {})
        assert signal["direction"] == "Neutral"

    def test_insufficient_samples(self):
        """Patterns with < 3 samples should be skipped."""
        pattern_stats = {
            "Up-Up": {"next_up": 2, "next_down": 0, "total": 2, "p_up": 1.0, "p_down": 0.0},
        }
        signal = get_pattern_signal(["Up", "Up"], pattern_stats)
        assert signal["direction"] == "Neutral"


# ---------------------------------------------------------------------------
# _compute_streaks
# ---------------------------------------------------------------------------

class TestStreaks:
    def test_all_up(self):
        s = _compute_streaks(["Up"] * 5)
        assert s["current_streak"] == 5
        assert s["current_direction"] == "Up"
        assert s["max_up_streak"] == 5
        assert s["max_down_streak"] == 0

    def test_alternating(self):
        s = _compute_streaks(["Up", "Down", "Up", "Down"])
        assert s["current_streak"] == 1
        assert s["max_up_streak"] == 1
        assert s["max_down_streak"] == 1
        assert s["avg_streak_length"] == 1.0

    def test_empty(self):
        s = _compute_streaks([])
        assert s["current_streak"] == 0
        assert s["current_direction"] is None


# ---------------------------------------------------------------------------
# load_outcome_sequence (with temp directory)
# ---------------------------------------------------------------------------

class TestLoadOutcomes:
    def test_loads_and_sorts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = _make_rounds_dir(tmpdir, [
                (300, "Up"),
                (100, "Down"),
                (200, "Up"),
            ])
            seq = load_outcome_sequence(data_dir)
            assert len(seq) == 3
            assert seq[0]["timestamp"] == 100
            assert seq[0]["outcome"] == "Down"
            assert seq[1]["timestamp"] == 200
            assert seq[2]["timestamp"] == 300

    def test_skips_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = _make_rounds_dir(tmpdir, [(100, "Up")])
            # Add an invalid result file
            bad_dir = os.path.join(data_dir, "rounds", "999")
            os.makedirs(bad_dir, exist_ok=True)
            with open(os.path.join(bad_dir, "result.json"), "w") as f:
                f.write("{invalid json")
            seq = load_outcome_sequence(data_dir)
            assert len(seq) == 1

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            os.makedirs(data_dir, exist_ok=True)
            seq = load_outcome_sequence(data_dir)
            assert seq == []

    def test_has_hour_and_weekday(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # timestamp 0 = 1970-01-01 00:00:00 UTC (Thursday)
            data_dir = _make_rounds_dir(tmpdir, [(0, "Up")])
            seq = load_outcome_sequence(data_dir)
            assert seq[0]["hour_utc"] == 0
            assert seq[0]["weekday"] == "Thursday"


# ---------------------------------------------------------------------------
# run_full_analysis (integration)
# ---------------------------------------------------------------------------

class TestRunFullAnalysis:
    def test_full_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rounds = [(i * 300, "Up" if i % 3 != 0 else "Down") for i in range(30)]
            data_dir = _make_rounds_dir(tmpdir, rounds)
            result = run_full_analysis(data_dir)

            assert result["total_rounds"] == 30
            assert result["up_count"] + result["down_count"] == 30
            assert "autocorrelation" in result
            assert "pattern_stats" in result
            assert "time_patterns" in result
            assert "streaks" in result
            assert "current_signal" in result
            assert "last_10_outcomes" in result
            assert len(result["last_10_outcomes"]) == 10

            # Check file was saved
            output_path = os.path.join(data_dir, "outcome_analysis.json")
            assert os.path.exists(output_path)
            with open(output_path) as f:
                saved = json.load(f)
            assert saved["total_rounds"] == 30

    def test_empty_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            os.makedirs(data_dir, exist_ok=True)
            result = run_full_analysis(data_dir)
            assert result["total_rounds"] == 0

            # Still saves the file
            output_path = os.path.join(data_dir, "outcome_analysis.json")
            assert os.path.exists(output_path)


# ---------------------------------------------------------------------------
# build_outcome_context
# ---------------------------------------------------------------------------

class TestBuildOutcomeContext:
    def test_produces_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rounds = [(i * 300, "Up" if i % 2 == 0 else "Down") for i in range(50)]
            data_dir = _make_rounds_dir(tmpdir, rounds)
            ctx = build_outcome_context(data_dir)
            assert "Outcome Pattern Analysis" in ctx
            assert "Overall Up rate" in ctx
            assert "Autocorrelation" in ctx
            assert "Last 10" in ctx

    def test_empty_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            os.makedirs(data_dir, exist_ok=True)
            ctx = build_outcome_context(data_dir)
            assert ctx == ""

    def test_too_few_rounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = _make_rounds_dir(tmpdir, [(100, "Up"), (200, "Down")])
            ctx = build_outcome_context(data_dir)
            assert ctx == ""
