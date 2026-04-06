"""
Outcome Analyzer — analyze BTC 5-minute outcome sequences for autocorrelation,
streak patterns, time-of-day effects, and day-of-week effects.

Produces a prediction signal and human-readable context for agent prompt injection.
"""
import glob
import json
import logging
import os
from datetime import datetime

from src.io_utils import atomic_write_json

logger = logging.getLogger(__name__)


def load_outcome_sequence(data_dir: str) -> list[dict]:
    """Load all outcomes chronologically.

    Each dict has 'timestamp', 'outcome', 'hour_utc', 'weekday'.
    """
    pattern = os.path.join(data_dir, "rounds", "*", "result.json")
    result_files = glob.glob(pattern)
    entries = []
    for fpath in result_files:
        try:
            with open(fpath) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        outcome = data.get("outcome")
        ts = data.get("round_timestamp")
        if outcome not in ("Up", "Down") or ts is None:
            continue

        dt = datetime.utcfromtimestamp(int(ts))
        entries.append({
            "timestamp": int(ts),
            "outcome": outcome,
            "hour_utc": dt.hour,
            "weekday": dt.strftime("%A"),
        })

    entries.sort(key=lambda e: e["timestamp"])
    return entries


def compute_autocorrelation(outcomes: list[str], max_lag: int = 10) -> dict[int, float]:
    """Compute autocorrelation at various lags.

    Encode Up=+1, Down=-1. Autocorrelation at lag k is the Pearson correlation
    between the series and its k-lagged version.

    +1 = perfect streak continuation, -1 = perfect alternation, 0 = random.
    """
    n = len(outcomes)
    if n < 2:
        return {}

    # Encode
    values = [1.0 if o == "Up" else -1.0 for o in outcomes]

    # Mean and variance
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    if var == 0:
        # All same outcome — autocorrelation is 1 at all lags by convention
        return {lag: 1.0 for lag in range(1, min(max_lag + 1, n))}

    result = {}
    for lag in range(1, min(max_lag + 1, n)):
        cov = sum(
            (values[i] - mean) * (values[i + lag] - mean)
            for i in range(n - lag)
        ) / (n - lag)
        result[lag] = round(cov / var, 4)

    return result


def analyze_patterns(
    outcomes: list[str], pattern_lengths: list[int] | None = None
) -> dict[str, dict]:
    """For each n-gram pattern, compute P(next=Up) and P(next=Down) with counts.

    Returns a dict keyed by pattern string (e.g. "Up-Down") with values:
      {"next_up": int, "next_down": int, "total": int, "p_up": float, "p_down": float}
    """
    if pattern_lengths is None:
        pattern_lengths = [2, 3]

    stats: dict[str, dict] = {}

    for length in pattern_lengths:
        if len(outcomes) <= length:
            continue
        for i in range(len(outcomes) - length):
            pattern_key = "-".join(outcomes[i : i + length])
            next_outcome = outcomes[i + length]

            if pattern_key not in stats:
                stats[pattern_key] = {"next_up": 0, "next_down": 0, "total": 0}

            entry = stats[pattern_key]
            if next_outcome == "Up":
                entry["next_up"] += 1
            else:
                entry["next_down"] += 1
            entry["total"] += 1

    # Compute probabilities
    for entry in stats.values():
        total = entry["total"]
        entry["p_up"] = round(entry["next_up"] / total, 4) if total > 0 else 0.5
        entry["p_down"] = round(entry["next_down"] / total, 4) if total > 0 else 0.5

    return stats


def analyze_time_patterns(sequence: list[dict]) -> dict:
    """Compute Up rate by hour-of-day and day-of-week."""
    hourly: dict[int, dict] = {}
    daily: dict[str, dict] = {}

    for entry in sequence:
        hour = entry["hour_utc"]
        weekday = entry["weekday"]
        outcome = entry["outcome"]

        if hour not in hourly:
            hourly[hour] = {"up": 0, "down": 0, "total": 0}
        hourly[hour]["total"] += 1
        if outcome == "Up":
            hourly[hour]["up"] += 1
        else:
            hourly[hour]["down"] += 1

        if weekday not in daily:
            daily[weekday] = {"up": 0, "down": 0, "total": 0}
        daily[weekday]["total"] += 1
        if outcome == "Up":
            daily[weekday]["up"] += 1
        else:
            daily[weekday]["down"] += 1

    # Compute rates
    hourly_rates = {}
    for hour in sorted(hourly):
        h = hourly[hour]
        hourly_rates[hour] = {
            "up_rate": round(h["up"] / h["total"], 4) if h["total"] > 0 else 0.5,
            "total": h["total"],
        }

    daily_rates = {}
    for day, d in daily.items():
        daily_rates[day] = {
            "up_rate": round(d["up"] / d["total"], 4) if d["total"] > 0 else 0.5,
            "total": d["total"],
        }

    return {"hourly": hourly_rates, "daily": daily_rates}


def _compute_streaks(outcomes: list[str]) -> dict:
    """Compute streak statistics."""
    if not outcomes:
        return {"current_streak": 0, "current_direction": None,
                "max_up_streak": 0, "max_down_streak": 0,
                "avg_streak_length": 0.0}

    streaks = []
    current_dir = outcomes[0]
    current_len = 1
    max_up = 0
    max_down = 0

    for i in range(1, len(outcomes)):
        if outcomes[i] == current_dir:
            current_len += 1
        else:
            streaks.append((current_dir, current_len))
            if current_dir == "Up":
                max_up = max(max_up, current_len)
            else:
                max_down = max(max_down, current_len)
            current_dir = outcomes[i]
            current_len = 1

    # Final streak
    streaks.append((current_dir, current_len))
    if current_dir == "Up":
        max_up = max(max_up, current_len)
    else:
        max_down = max(max_down, current_len)

    avg_len = sum(s[1] for s in streaks) / len(streaks) if streaks else 0.0

    return {
        "current_streak": current_len,
        "current_direction": current_dir,
        "max_up_streak": max_up,
        "max_down_streak": max_down,
        "avg_streak_length": round(avg_len, 2),
    }


def get_pattern_signal(outcomes: list[str], pattern_stats: dict) -> dict:
    """Given recent outcomes, look up the pattern and return predicted direction + confidence.

    Returns:
      {"direction": "Up"/"Down"/"Neutral", "confidence": float, "pattern": str, "sample_size": int}
    """
    if not outcomes or not pattern_stats:
        return {"direction": "Neutral", "confidence": 0.0, "pattern": "", "sample_size": 0}

    # Try longest pattern first (3-gram), then shorter (2-gram)
    for length in [3, 2]:
        if len(outcomes) >= length:
            recent = outcomes[-length:]
            pattern_key = "-".join(recent)
            if pattern_key in pattern_stats:
                stats = pattern_stats[pattern_key]
                p_up = stats["p_up"]
                p_down = stats["p_down"]
                sample_size = stats["total"]

                # Need minimum samples for signal
                if sample_size < 3:
                    continue

                bias = abs(p_up - p_down)
                direction = "Up" if p_up > p_down else "Down" if p_down > p_up else "Neutral"

                return {
                    "direction": direction,
                    "confidence": round(bias, 4),
                    "pattern": pattern_key,
                    "sample_size": sample_size,
                }

    return {"direction": "Neutral", "confidence": 0.0, "pattern": "", "sample_size": 0}


def build_outcome_context(data_dir: str) -> str:
    """Build human-readable summary for agent prompts."""
    sequence = load_outcome_sequence(data_dir)
    if not sequence:
        return ""

    outcomes = [e["outcome"] for e in sequence]
    n = len(outcomes)
    if n < 5:
        return ""

    # Autocorrelation
    ac = compute_autocorrelation(outcomes, max_lag=5)

    # Pattern analysis
    pattern_stats = analyze_patterns(outcomes)

    # Time patterns
    time_patterns = analyze_time_patterns(sequence)

    # Streaks
    streaks = _compute_streaks(outcomes)

    # Current signal
    signal = get_pattern_signal(outcomes, pattern_stats)

    # Overall up rate
    up_count = outcomes.count("Up")
    up_rate = round(up_count / n, 4)

    # Build readable context
    lines = [
        "## Outcome Pattern Analysis",
        f"Based on {n} historical rounds.",
        "",
        f"**Overall Up rate:** {up_rate:.1%} ({up_count}/{n})",
        "",
    ]

    # Autocorrelation summary
    if ac:
        ac1 = ac.get(1, 0)
        if ac1 > 0.1:
            lines.append(f"**Autocorrelation (lag-1):** {ac1:+.3f} (momentum/trending)")
        elif ac1 < -0.1:
            lines.append(f"**Autocorrelation (lag-1):** {ac1:+.3f} (mean-reverting)")
        else:
            lines.append(f"**Autocorrelation (lag-1):** {ac1:+.3f} (near random)")

    # Streaks
    lines.append("")
    lines.append(f"**Current streak:** {streaks['current_streak']} {streaks['current_direction']}")
    lines.append(f"**Max Up streak:** {streaks['max_up_streak']} | **Max Down streak:** {streaks['max_down_streak']}")
    lines.append(f"**Avg streak length:** {streaks['avg_streak_length']:.1f}")

    # Last 10 outcomes
    last_10 = outcomes[-10:]
    lines.append("")
    lines.append(f"**Last 10:** {' '.join(last_10)}")

    # Pattern signal
    if signal["direction"] != "Neutral" and signal["confidence"] > 0.05:
        lines.append("")
        lines.append(f"**Pattern signal:** After `{signal['pattern']}`, next is {signal['direction']} "
                      f"({signal['confidence']:.0%} edge, n={signal['sample_size']})")

    # Top time-of-day biases
    hourly = time_patterns.get("hourly", {})
    extreme_hours = [
        (h, info)
        for h, info in hourly.items()
        if info["total"] >= 5 and abs(info["up_rate"] - 0.5) > 0.1
    ]
    if extreme_hours:
        extreme_hours.sort(key=lambda x: abs(x[1]["up_rate"] - 0.5), reverse=True)
        lines.append("")
        lines.append("**Notable hour-of-day biases (UTC):**")
        for h, info in extreme_hours[:5]:
            direction = "Up-biased" if info["up_rate"] > 0.5 else "Down-biased"
            lines.append(f"  - {h:02d}:00 — {info['up_rate']:.0%} Up ({direction}, n={info['total']})")

    # Top day-of-week biases
    daily = time_patterns.get("daily", {})
    extreme_days = [
        (d, info)
        for d, info in daily.items()
        if info["total"] >= 5 and abs(info["up_rate"] - 0.5) > 0.1
    ]
    if extreme_days:
        extreme_days.sort(key=lambda x: abs(x[1]["up_rate"] - 0.5), reverse=True)
        lines.append("")
        lines.append("**Notable day-of-week biases:**")
        for d, info in extreme_days[:5]:
            direction = "Up-biased" if info["up_rate"] > 0.5 else "Down-biased"
            lines.append(f"  - {d} — {info['up_rate']:.0%} Up ({direction}, n={info['total']})")

    return "\n".join(lines)


def run_full_analysis(data_dir: str) -> dict:
    """Run everything and save to data/outcome_analysis.json. Return the full analysis."""
    sequence = load_outcome_sequence(data_dir)
    if not sequence:
        result = {"total_rounds": 0, "error": "No outcome data found"}
        output_path = os.path.join(data_dir, "outcome_analysis.json")
        atomic_write_json(output_path, result)
        return result

    outcomes = [e["outcome"] for e in sequence]
    n = len(outcomes)

    up_count = outcomes.count("Up")

    autocorrelation = compute_autocorrelation(outcomes, max_lag=10)
    pattern_stats = analyze_patterns(outcomes, pattern_lengths=[2, 3])
    time_patterns = analyze_time_patterns(sequence)
    streaks = _compute_streaks(outcomes)
    signal = get_pattern_signal(outcomes, pattern_stats)

    # Sort pattern stats by total count descending for readability
    sorted_patterns = dict(
        sorted(pattern_stats.items(), key=lambda kv: kv[1]["total"], reverse=True)
    )

    result = {
        "total_rounds": n,
        "up_count": up_count,
        "down_count": n - up_count,
        "up_rate": round(up_count / n, 4),
        "autocorrelation": {str(k): v for k, v in autocorrelation.items()},
        "streaks": streaks,
        "pattern_stats": sorted_patterns,
        "time_patterns": time_patterns,
        "current_signal": signal,
        "last_10_outcomes": outcomes[-10:],
        "analyzed_at": int(datetime.utcnow().timestamp()),
    }

    output_path = os.path.join(data_dir, "outcome_analysis.json")
    atomic_write_json(output_path, result)
    logger.info(f"Outcome analysis saved to {output_path} ({n} rounds)")

    return result
