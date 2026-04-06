# src/runner/agent_correlation.py
"""
Agent Correlation Analysis — compute pairwise prediction correlation
to improve ensemble diversity and strategy evolution.

For each round, gets each agent's final prediction (Up=1, Down=0),
then computes agreement rates, Pearson correlations, diversity clusters,
and recommends diverse ensemble members.
"""
import logging
import math
import os

from src.io_utils import atomic_write_json, read_jsonl

logger = logging.getLogger(__name__)


def build_prediction_matrix(
    agents_dir: str,
) -> tuple[list[str], list[int], dict[str, dict[int, int]]]:
    """Returns (agent_names, round_timestamps, predictions_map).

    predictions_map[agent][round] = 1 (Up) or 0 (Down).
    Only scored rounds (correct is not None) are included.
    For each round, the LAST prediction is used (highest revision or last occurrence).
    """
    if not os.path.isdir(agents_dir):
        return [], [], {}

    predictions_map: dict[str, dict[int, int]] = {}
    all_rounds: set[int] = set()

    for name in sorted(os.listdir(agents_dir)):
        if not name.startswith("agent-"):
            continue
        pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
        preds = read_jsonl(pred_path)
        if not preds:
            continue

        # For each round, keep the last prediction where correct is not None.
        # "Last" means highest revision, or last occurrence if revisions are equal.
        round_best: dict[int, dict] = {}
        for p in preds:
            rnd = p.get("round")
            if rnd is None:
                continue
            if p.get("correct") is None:
                continue
            prev = round_best.get(rnd)
            if prev is None or p.get("revision", 0) >= prev.get("revision", 0):
                round_best[rnd] = p

        if not round_best:
            continue

        agent_preds: dict[int, int] = {}
        for rnd, p in round_best.items():
            direction = p.get("prediction", "").strip().lower()
            if direction == "up":
                agent_preds[rnd] = 1
            elif direction == "down":
                agent_preds[rnd] = 0
            else:
                continue  # skip unknown predictions

        if agent_preds:
            predictions_map[name] = agent_preds
            all_rounds.update(agent_preds.keys())

    agent_names = sorted(predictions_map.keys())
    rounds = sorted(all_rounds)
    return agent_names, rounds, predictions_map


def compute_agreement_matrix(
    agent_names: list[str],
    rounds: list[int],
    preds: dict[str, dict[int, int]],
) -> dict[tuple[str, str], float]:
    """Pairwise agreement rate. Only counts rounds where both agents predicted.

    Returns dict of (agent_a, agent_b) -> agreement_rate in [0, 1].
    """
    agreement: dict[tuple[str, str], float] = {}

    for i, a in enumerate(agent_names):
        for j, b in enumerate(agent_names):
            if i >= j:
                continue  # only upper triangle; skip self-pairs
            a_preds = preds.get(a, {})
            b_preds = preds.get(b, {})

            # Find overlapping rounds
            common = [r for r in rounds if r in a_preds and r in b_preds]
            if not common:
                agreement[(a, b)] = 0.0
                continue

            same = sum(1 for r in common if a_preds[r] == b_preds[r])
            agreement[(a, b)] = same / len(common)

    return agreement


def compute_correlation_matrix(
    agent_names: list[str],
    rounds: list[int],
    preds: dict[str, dict[int, int]],
) -> dict[tuple[str, str], float]:
    """Pearson correlation on binary predictions.

    Returns dict of (agent_a, agent_b) -> correlation in [-1, 1].
    Uses formula: r = (n*sum(xy) - sum(x)*sum(y)) /
                       sqrt((n*sum(x^2) - sum(x)^2) * (n*sum(y^2) - sum(y)^2))
    Division by zero (constant predictions) -> correlation = 0.
    """
    correlations: dict[tuple[str, str], float] = {}

    for i, a in enumerate(agent_names):
        for j, b in enumerate(agent_names):
            if i >= j:
                continue
            a_preds = preds.get(a, {})
            b_preds = preds.get(b, {})

            common = [r for r in rounds if r in a_preds and r in b_preds]
            if not common:
                correlations[(a, b)] = 0.0
                continue

            n = len(common)
            xs = [a_preds[r] for r in common]
            ys = [b_preds[r] for r in common]

            sum_x = sum(xs)
            sum_y = sum(ys)
            sum_xy = sum(x * y for x, y in zip(xs, ys))
            sum_x2 = sum(x * x for x in xs)
            sum_y2 = sum(y * y for y in ys)

            numerator = n * sum_xy - sum_x * sum_y
            denom_x = n * sum_x2 - sum_x * sum_x
            denom_y = n * sum_y2 - sum_y * sum_y

            if denom_x <= 0 or denom_y <= 0:
                correlations[(a, b)] = 0.0
            else:
                correlations[(a, b)] = numerator / math.sqrt(denom_x * denom_y)

    return correlations


def _compute_win_rates(
    agent_names: list[str],
    agents_dir: str,
) -> dict[str, float]:
    """Compute win rates for each agent from their predictions."""
    win_rates: dict[str, float] = {}
    for name in agent_names:
        pred_path = os.path.join(agents_dir, name, "predictions.jsonl")
        preds = read_jsonl(pred_path)
        scored = [p for p in preds if p.get("correct") is not None]
        if scored:
            wins = sum(1 for p in scored if p["correct"])
            win_rates[name] = wins / len(scored)
        else:
            win_rates[name] = 0.0
    return win_rates


def find_diverse_ensemble(
    agent_names: list[str],
    correlations: dict[tuple[str, str], float],
    win_rates: dict[str, float],
    min_wr: float = 0.48,
    size: int = 5,
) -> list[str]:
    """Greedy selection: start with best WR agent, add agents with lowest
    avg correlation to current set.

    Only considers agents above min_wr threshold.
    """
    eligible = [a for a in agent_names if win_rates.get(a, 0) >= min_wr]
    if not eligible:
        return []

    # Start with highest WR agent
    eligible_sorted = sorted(eligible, key=lambda a: -win_rates.get(a, 0))
    ensemble = [eligible_sorted[0]]
    remaining = set(eligible_sorted[1:])

    while len(ensemble) < size and remaining:
        best_candidate = None
        best_avg_corr = float("inf")

        for candidate in remaining:
            # Compute average correlation with current ensemble
            corrs = []
            for member in ensemble:
                pair = tuple(sorted([candidate, member]))
                corrs.append(correlations.get(pair, 0.0))
            avg_corr = sum(corrs) / len(corrs) if corrs else 0.0

            if avg_corr < best_avg_corr:
                best_avg_corr = avg_corr
                best_candidate = candidate

        if best_candidate is None:
            break

        ensemble.append(best_candidate)
        remaining.discard(best_candidate)

    return ensemble


def find_diversity_clusters(
    agent_names: list[str],
    correlations: dict[tuple[str, str], float],
    threshold: float = 0.5,
) -> list[list[str]]:
    """Group agents by correlation. Agents with correlation > threshold
    are placed in the same cluster. Simple greedy clustering."""
    assigned: set[str] = set()
    clusters: list[list[str]] = []

    for agent in agent_names:
        if agent in assigned:
            continue
        cluster = [agent]
        assigned.add(agent)

        for other in agent_names:
            if other in assigned:
                continue
            pair = tuple(sorted([agent, other]))
            corr = correlations.get(pair, 0.0)
            if corr > threshold:
                cluster.append(other)
                assigned.add(other)

        clusters.append(cluster)

    return clusters


def build_correlation_context(agents_dir: str) -> str:
    """Human-readable correlation summary for injection into prompts."""
    agent_names, rounds, preds = build_prediction_matrix(agents_dir)
    if len(agent_names) < 2:
        return ""

    correlations = compute_correlation_matrix(agent_names, rounds, preds)
    agreement = compute_agreement_matrix(agent_names, rounds, preds)
    win_rates = _compute_win_rates(agent_names, agents_dir)
    clusters = find_diversity_clusters(agent_names, correlations)
    ensemble = find_diverse_ensemble(agent_names, correlations, win_rates)

    lines = [
        "## Agent Prediction Correlation",
        "",
        f"Based on {len(rounds)} scored rounds across {len(agent_names)} agents.",
        "",
    ]

    # Top correlated pairs
    sorted_corr = sorted(correlations.items(), key=lambda x: -abs(x[1]))
    if sorted_corr:
        lines.append("### Most Correlated Pairs (predict similarly)")
        for (a, b), c in sorted_corr[:5]:
            agr = agreement.get((a, b), agreement.get((b, a), 0.0))
            lines.append(f"- {a} <-> {b}: correlation={c:.2f}, agreement={agr:.0%}")
        lines.append("")

    # Most independent pairs
    independent = sorted(correlations.items(), key=lambda x: abs(x[1]))
    if independent:
        lines.append("### Most Independent Pairs (diverse predictions)")
        for (a, b), c in independent[:5]:
            agr = agreement.get((a, b), agreement.get((b, a), 0.0))
            lines.append(f"- {a} <-> {b}: correlation={c:.2f}, agreement={agr:.0%}")
        lines.append("")

    # Clusters
    if clusters:
        lines.append("### Diversity Clusters")
        for i, cluster in enumerate(clusters, 1):
            lines.append(f"- Cluster {i}: {', '.join(cluster)}")
        lines.append("")

    # Recommended ensemble
    if ensemble:
        lines.append("### Recommended Diverse Ensemble")
        lines.append(f"- Members: {', '.join(ensemble)}")
        avg_wr = sum(win_rates.get(a, 0) for a in ensemble) / len(ensemble)
        lines.append(f"- Average WR: {avg_wr:.1%}")
        lines.append("")

    return "\n".join(lines)


def run_full_analysis(agents_dir: str) -> dict:
    """Run full correlation analysis and return results dict.

    Also saves results to agents_dir/../data/agent_correlations.json.
    """
    agent_names, rounds, preds = build_prediction_matrix(agents_dir)

    if len(agent_names) < 2:
        return {"agent_count": len(agent_names), "round_count": len(rounds)}

    agreement = compute_agreement_matrix(agent_names, rounds, preds)
    correlations = compute_correlation_matrix(agent_names, rounds, preds)
    win_rates = _compute_win_rates(agent_names, agents_dir)
    clusters = find_diversity_clusters(agent_names, correlations)
    ensemble = find_diverse_ensemble(agent_names, correlations, win_rates)

    # Convert tuple keys to strings for JSON serialization
    agreement_json = {f"{a}|{b}": v for (a, b), v in agreement.items()}
    correlation_json = {f"{a}|{b}": v for (a, b), v in correlations.items()}

    result = {
        "agent_count": len(agent_names),
        "round_count": len(rounds),
        "agents": agent_names,
        "agreement_matrix": agreement_json,
        "correlation_matrix": correlation_json,
        "win_rates": win_rates,
        "clusters": clusters,
        "recommended_ensemble": ensemble,
    }

    # Save to data directory
    data_dir = os.path.join(os.path.dirname(agents_dir.rstrip("/")), "data")
    output_path = os.path.join(data_dir, "agent_correlations.json")
    try:
        atomic_write_json(output_path, result)
        logger.info(f"Correlation analysis saved to {output_path}")
    except Exception as e:
        logger.warning(f"Could not save correlation analysis: {e}")

    return result
