# src/runner/decision_tracker.py
"""
Decision Quality Tracker — analyzes intraround prediction revisions.

Tracks:
- Win rate by revision number (is the 2nd call better than the 4th?)
- Decision flip events (when agents change direction mid-round)
- Flip quality (did flipping improve or hurt accuracy?)
- Optimal decision timing (which age_seconds produces best results?)
- Per-agent decision profiles fed back into evolution prompts

Data flow:
  prediction-updates/{agent}.jsonl  →  analyze  →  decision_quality.json (per agent)
                                                →  decision_insights (shared knowledge)
"""
import json
import logging
import os
import time

from src.io_utils import atomic_write_json, read_jsonl

logger = logging.getLogger(__name__)


def _load_round_updates(data_dir: str, round_timestamp: int, agent_name: str) -> list[dict]:
    """Load all prediction revisions for an agent in a round."""
    path = os.path.join(
        data_dir, "rounds", str(round_timestamp),
        "prediction-updates", f"{agent_name}.jsonl",
    )
    return read_jsonl(path)


def _load_all_round_updates(data_dir: str, agent_name: str) -> dict[int, list[dict]]:
    """Load all prediction updates across all rounds for an agent."""
    rounds_dir = os.path.join(data_dir, "rounds")
    if not os.path.isdir(rounds_dir):
        return {}

    by_round: dict[int, list[dict]] = {}
    for dirname in sorted(os.listdir(rounds_dir)):
        if not dirname.isdigit():
            continue
        round_ts = int(dirname)
        updates = _load_round_updates(data_dir, round_ts, agent_name)
        if updates:
            by_round[round_ts] = updates
    return by_round


def _get_outcome(data_dir: str, round_timestamp: int) -> str | None:
    """Get the resolved outcome for a round."""
    result_path = os.path.join(data_dir, "rounds", str(round_timestamp), "result.json")
    if not os.path.exists(result_path):
        return None
    try:
        with open(result_path) as f:
            return json.load(f).get("outcome")
    except (json.JSONDecodeError, OSError):
        return None


def analyze_agent_decisions(data_dir: str, agent_name: str) -> dict:
    """
    Analyze decision quality for a single agent across all rounds.

    Returns a comprehensive decision profile:
    - revision_stats: WR by revision number
    - flip_stats: flip frequency, flip accuracy
    - timing_stats: accuracy by prediction age
    - optimal_revision: which revision performs best
    - decision_profile: summary for injection into evolution prompts
    """
    all_updates = _load_all_round_updates(data_dir, agent_name)
    if not all_updates:
        return {"agent": agent_name, "rounds_analyzed": 0}

    # Per-revision accuracy
    revision_correct: dict[int, list[bool]] = {}  # revision_num -> [correct, correct, ...]
    # Flip tracking
    flip_events: list[dict] = []
    # Timing tracking
    timing_buckets: dict[str, list[bool]] = {}  # "early"/"mid"/"late" -> [correct, ...]
    # First vs last accuracy
    first_correct: list[bool] = []
    last_correct: list[bool] = []
    # Rounds with flips vs without
    flip_round_correct: list[bool] = []
    no_flip_round_correct: list[bool] = []

    rounds_analyzed = 0

    for round_ts, updates in all_updates.items():
        outcome = _get_outcome(data_dir, round_ts)
        if not outcome or not updates:
            continue

        rounds_analyzed += 1

        # Score each revision
        for update in updates:
            rev = update.get("revision", 1)
            pred = update.get("prediction")
            if not pred:
                continue
            correct = pred.strip().lower() == outcome.strip().lower()
            revision_correct.setdefault(rev, []).append(correct)

            # Timing bucket based on predicted_at relative to round start
            predicted_at = update.get("predicted_at")
            if predicted_at:
                age_ms = predicted_at - (round_ts * 1000)
                age_s = age_ms / 1000
                if age_s < 60:
                    bucket = "early_0-60s"
                elif age_s < 180:
                    bucket = "mid_60-180s"
                else:
                    bucket = "late_180-300s"
                timing_buckets.setdefault(bucket, []).append(correct)

        # First and last prediction
        first_pred = updates[0].get("prediction", "").strip().lower()
        last_pred = updates[-1].get("prediction", "").strip().lower()
        outcome_lower = outcome.strip().lower()

        first_correct.append(first_pred == outcome_lower)
        last_correct.append(last_pred == outcome_lower)

        # Detect flips
        predictions_sequence = [u.get("prediction") for u in updates if u.get("prediction")]
        flips_in_round = []
        for i in range(1, len(predictions_sequence)):
            prev = predictions_sequence[i - 1]
            curr = predictions_sequence[i]
            if prev and curr and prev.strip().lower() != curr.strip().lower():
                flip_event = {
                    "round": round_ts,
                    "from": prev,
                    "to": curr,
                    "revision": updates[i].get("revision", i + 1),
                    "age_seconds": None,
                    "flip_was_correct": curr.strip().lower() == outcome_lower,
                    "staying_was_correct": prev.strip().lower() == outcome_lower,
                }
                predicted_at = updates[i].get("predicted_at")
                if predicted_at:
                    flip_event["age_seconds"] = (predicted_at - round_ts * 1000) / 1000
                flips_in_round.append(flip_event)
                flip_events.append(flip_event)

        # Track whether rounds with flips do better or worse
        final_correct = last_pred == outcome_lower
        if flips_in_round:
            flip_round_correct.append(final_correct)
        else:
            no_flip_round_correct.append(final_correct)

    # Build revision stats
    revision_stats = {}
    for rev, results in sorted(revision_correct.items()):
        wins = sum(results)
        total = len(results)
        revision_stats[rev] = {
            "win_rate": wins / total if total else 0,
            "wins": wins,
            "total": total,
        }

    # Find optimal revision
    best_rev = None
    best_wr = 0
    for rev, stats in revision_stats.items():
        if stats["total"] >= 5 and stats["win_rate"] > best_wr:
            best_wr = stats["win_rate"]
            best_rev = rev

    # Build flip stats
    total_flips = len(flip_events)
    flips_helped = sum(1 for f in flip_events if f["flip_was_correct"])
    flips_hurt = sum(1 for f in flip_events if f["staying_was_correct"] and not f["flip_was_correct"])

    # Timing stats
    timing_stats = {}
    for bucket, results in sorted(timing_buckets.items()):
        wins = sum(results)
        total = len(results)
        timing_stats[bucket] = {
            "win_rate": wins / total if total else 0,
            "wins": wins,
            "total": total,
        }

    # First vs last comparison
    first_wr = sum(first_correct) / len(first_correct) if first_correct else 0
    last_wr = sum(last_correct) / len(last_correct) if last_correct else 0
    flip_round_wr = sum(flip_round_correct) / len(flip_round_correct) if flip_round_correct else 0
    no_flip_round_wr = sum(no_flip_round_correct) / len(no_flip_round_correct) if no_flip_round_correct else 0

    profile = {
        "agent": agent_name,
        "rounds_analyzed": rounds_analyzed,
        "total_revisions": sum(s["total"] for s in revision_stats.values()),
        "avg_revisions_per_round": (
            sum(s["total"] for s in revision_stats.values()) / rounds_analyzed
            if rounds_analyzed else 0
        ),
        "revision_stats": revision_stats,
        "optimal_revision": best_rev,
        "optimal_revision_wr": best_wr,
        "first_call_wr": first_wr,
        "last_call_wr": last_wr,
        "first_vs_last": "first" if first_wr > last_wr + 0.02 else "last" if last_wr > first_wr + 0.02 else "equal",
        "flip_stats": {
            "total_flips": total_flips,
            "flips_per_round": total_flips / rounds_analyzed if rounds_analyzed else 0,
            "flips_helped": flips_helped,
            "flips_hurt": flips_hurt,
            "flip_help_rate": flips_helped / total_flips if total_flips else 0,
            "flip_round_wr": flip_round_wr,
            "no_flip_round_wr": no_flip_round_wr,
            "flipping_is_beneficial": flip_round_wr > no_flip_round_wr + 0.03,
        },
        "timing_stats": timing_stats,
        "analyzed_at": int(time.time() * 1000),
    }

    # Build human-readable decision profile for injection into prompts
    profile["decision_profile"] = _build_decision_profile_text(profile)

    return profile


def _build_decision_profile_text(profile: dict) -> str:
    """Build a human-readable decision profile summary for agent prompts."""
    lines = [f"## Decision Quality Profile ({profile['rounds_analyzed']} rounds analyzed)"]

    # Revision accuracy
    rev_stats = profile.get("revision_stats", {})
    if rev_stats:
        lines.append("")
        lines.append("### Accuracy by Revision Number")
        for rev, stats in sorted(rev_stats.items(), key=lambda x: int(x[0])):
            lines.append(f"- Revision {rev}: {stats['win_rate']:.0%} ({stats['wins']}/{stats['total']})")

        best_rev = profile.get("optimal_revision")
        if best_rev:
            lines.append(f"- **Best revision: #{best_rev} ({profile['optimal_revision_wr']:.0%})**")

    # First vs last
    first_wr = profile.get("first_call_wr", 0)
    last_wr = profile.get("last_call_wr", 0)
    verdict = profile.get("first_vs_last", "equal")
    lines.append("")
    lines.append(f"### First vs Last Call")
    lines.append(f"- First call WR: {first_wr:.0%}")
    lines.append(f"- Last call WR: {last_wr:.0%}")
    if verdict == "first":
        lines.append(f"- **Your FIRST instinct is more accurate. Consider sticking with initial calls.**")
    elif verdict == "last":
        lines.append(f"- **Your LATER calls are more accurate. Additional data helps your decisions.**")
    else:
        lines.append(f"- First and last calls perform similarly.")

    # Flip analysis
    flip = profile.get("flip_stats", {})
    total_flips = flip.get("total_flips", 0)
    if total_flips > 0:
        lines.append("")
        lines.append("### Decision Flips")
        lines.append(f"- Total flips: {total_flips} ({flip.get('flips_per_round', 0):.1f}/round)")
        lines.append(f"- Flipping helped: {flip.get('flips_helped', 0)} times ({flip.get('flip_help_rate', 0):.0%})")
        lines.append(f"- Flipping hurt: {flip.get('flips_hurt', 0)} times")
        lines.append(f"- Rounds with flips WR: {flip.get('flip_round_wr', 0):.0%}")
        lines.append(f"- Rounds without flips WR: {flip.get('no_flip_round_wr', 0):.0%}")
        if flip.get("flipping_is_beneficial"):
            lines.append("- **Flipping is beneficial for you — updating your view with new data improves accuracy.**")
        elif flip.get("flip_round_wr", 0) < flip.get("no_flip_round_wr", 0) - 0.03:
            lines.append("- **Flipping HURTS your accuracy — consider committing to your initial read.**")
        else:
            lines.append("- Flipping has neutral impact on your accuracy.")

    # Timing
    timing = profile.get("timing_stats", {})
    if timing:
        lines.append("")
        lines.append("### Prediction Timing")
        for bucket, stats in sorted(timing.items()):
            label = bucket.replace("_", " ").replace("0-60s", "(0-60s)").replace("60-180s", "(60-180s)").replace("180-300s", "(180-300s)")
            lines.append(f"- {label}: {stats['win_rate']:.0%} ({stats['wins']}/{stats['total']})")

    return "\n".join(lines)


def save_agent_decision_profile(agents_dir: str, data_dir: str, agent_name: str) -> dict:
    """Analyze and save decision quality profile for an agent."""
    profile = analyze_agent_decisions(data_dir, agent_name)
    if profile.get("rounds_analyzed", 0) == 0:
        return profile

    output_path = os.path.join(agents_dir, agent_name, "decision_quality.json")
    # Save full profile (without the text, which can be regenerated)
    save_data = {k: v for k, v in profile.items() if k != "decision_profile"}
    atomic_write_json(output_path, save_data)

    logger.info(
        f"  {agent_name}: {profile['rounds_analyzed']}r analyzed, "
        f"first={profile['first_call_wr']:.0%} last={profile['last_call_wr']:.0%} "
        f"flips={profile.get('flip_stats', {}).get('total_flips', 0)} "
        f"best_rev={profile.get('optimal_revision')}"
    )
    return profile


def analyze_all_agents(agents_dir: str, data_dir: str) -> list[dict]:
    """Analyze decision quality for all agents."""
    profiles = []
    if not os.path.isdir(agents_dir):
        return profiles

    for name in sorted(os.listdir(agents_dir)):
        agent_dir = os.path.join(agents_dir, name)
        if not os.path.isdir(agent_dir) or not name.startswith("agent-"):
            continue
        profile = save_agent_decision_profile(agents_dir, data_dir, name)
        if profile.get("rounds_analyzed", 0) > 0:
            profiles.append(profile)

    return profiles


def build_decision_context_for_agent(agents_dir: str, agent_name: str) -> str:
    """Load the decision quality profile and return text for injection into prompts."""
    profile_path = os.path.join(agents_dir, agent_name, "decision_quality.json")
    if not os.path.exists(profile_path):
        return ""

    try:
        with open(profile_path) as f:
            profile = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    if profile.get("rounds_analyzed", 0) < 5:
        return ""

    return _build_decision_profile_text(profile)


def generate_decision_insights(profiles: list[dict]) -> str:
    """Generate shared knowledge insights from all agents' decision profiles."""
    if not profiles:
        return ""

    lines = ["# Decision Quality Insights (auto-generated)\n"]

    # Global: is first or last call better across all agents?
    first_better = sum(1 for p in profiles if p.get("first_vs_last") == "first")
    last_better = sum(1 for p in profiles if p.get("first_vs_last") == "last")
    equal = sum(1 for p in profiles if p.get("first_vs_last") == "equal")
    lines.append(f"## First vs Last Call (across {len(profiles)} agents)")
    lines.append(f"- First call better: {first_better} agents")
    lines.append(f"- Last call better: {last_better} agents")
    lines.append(f"- Equal: {equal} agents")
    if first_better > last_better * 1.5:
        lines.append("- **Consensus: initial instincts tend to be more accurate than revised calls.**")
    elif last_better > first_better * 1.5:
        lines.append("- **Consensus: later calls with more data tend to be more accurate.**")
    lines.append("")

    # Global: does flipping help?
    flip_beneficial = sum(1 for p in profiles if p.get("flip_stats", {}).get("flipping_is_beneficial"))
    flip_harmful = sum(
        1 for p in profiles
        if p.get("flip_stats", {}).get("flip_round_wr", 0.5) < p.get("flip_stats", {}).get("no_flip_round_wr", 0.5) - 0.03
    )
    lines.append(f"## Flipping Analysis")
    lines.append(f"- Flipping beneficial: {flip_beneficial} agents")
    lines.append(f"- Flipping harmful: {flip_harmful} agents")
    lines.append("")

    # Best revision across agents
    best_revs = [p.get("optimal_revision") for p in profiles if p.get("optimal_revision")]
    if best_revs:
        from collections import Counter
        rev_counts = Counter(best_revs)
        most_common_rev, count = rev_counts.most_common(1)[0]
        lines.append(f"## Optimal Revision")
        lines.append(f"- Most common best revision: #{most_common_rev} ({count}/{len(best_revs)} agents)")
        for rev, cnt in rev_counts.most_common():
            lines.append(f"  - Revision {rev}: best for {cnt} agents")
        lines.append("")

    # Timing
    early_wrs = [p["timing_stats"]["early_0-60s"]["win_rate"] for p in profiles if "early_0-60s" in p.get("timing_stats", {})]
    mid_wrs = [p["timing_stats"]["mid_60-180s"]["win_rate"] for p in profiles if "mid_60-180s" in p.get("timing_stats", {})]
    late_wrs = [p["timing_stats"]["late_180-300s"]["win_rate"] for p in profiles if "late_180-300s" in p.get("timing_stats", {})]
    if early_wrs or mid_wrs or late_wrs:
        lines.append("## Timing Analysis (avg WR across agents)")
        if early_wrs:
            lines.append(f"- Early (0-60s): {sum(early_wrs)/len(early_wrs):.0%} ({len(early_wrs)} agents)")
        if mid_wrs:
            lines.append(f"- Mid (60-180s): {sum(mid_wrs)/len(mid_wrs):.0%} ({len(mid_wrs)} agents)")
        if late_wrs:
            lines.append(f"- Late (180-300s): {sum(late_wrs)/len(late_wrs):.0%} ({len(late_wrs)} agents)")

    return "\n".join(lines)
