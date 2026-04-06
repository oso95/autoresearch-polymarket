#!/usr/bin/env python3
"""
Strategy Factory v2 — Continuous optimization loop that never stops improving.

The factory runs alongside live trading and orchestrates 10 phases per cycle:

 1. ANALYZE      — Deep stats with regime detection and trend analysis
 2. HARVEST      — Mine shared knowledge forum for actionable patterns
 3. BACKTEST     — Walk-forward validation across market regimes
 4. IDENTIFY     — Regime-aware targeting of evolution candidates
 5. EVOLVE       — Knowledge-enriched evolution with backtest validation
 6. CROSS-POLLINATE — Transfer winning scripts/traits to underperformers
 7. SYNTHESIZE   — Spawn new agents from top-voted forum discoveries
 8. ENSEMBLE     — Rebuild ensembles from current top performers
 9. PRUNE        — Mirror anti-predictive agents, retire hopeless ones
10. PUBLISH      — Post cycle findings back to shared knowledge forum

Runs forever. Every cycle reads the latest shared knowledge, backtests,
evolves, cross-pollinates, and publishes back — a closed feedback loop.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from src.codex_cli import DEFAULT_PREDICTION_MODEL, normalize_model_name
from src.runner.backtester import (
    Backtester,
    load_historical_rounds,
    split_train_test,
    walk_forward_splits,
)
from src.runner.evolver import StrategyEvolver
from src.runner.agent_runner import AgentRunner
from src.runner.ensemble import create_ensemble_agent, get_agent_win_rates
from src.coordinator.spawner import AgentSpawner
from src.coordinator.crossover import CrossPollinator
from src.shared_knowledge import SharedKnowledgeForum, ensure_shared_knowledge_forum
from src.runner.decision_tracker import analyze_all_agents, generate_decision_insights
from src.runner.outcome_analyzer import run_full_analysis, build_outcome_context
from src.runner.agent_correlation import run_full_analysis as run_correlation_analysis, build_correlation_context
from src.config import load_config
from src.io_utils import read_jsonl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FACTORY] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_DIR = os.environ.get("FACTORY_PROJECT_DIR", "./live-run")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
AGENTS_DIR = os.path.join(PROJECT_DIR, "agents")
SHARED_DIR = os.path.join(DATA_DIR, "shared_knowledge")

# Cycle timing
CYCLE_INTERVAL_SECONDS = int(os.environ.get("FACTORY_CYCLE_SECONDS", "600"))
ACCELERATED_INTERVAL = 120  # Faster cycle when changes detected

# Backtest
MIN_ROUNDS_FOR_BACKTEST = 30
WALK_FORWARD_TRAIN_SIZE = 40
WALK_FORWARD_TEST_SIZE = 15
WALK_FORWARD_STEP = 10
OVERFIT_THRESHOLD = 0.15

# Evolution
EVOLVE_THRESHOLD_WR = 0.45
EVOLVE_MIN_ROUNDS = 15
MAX_EVOLVE_PER_CYCLE = 4
EVOLVE_TIMEOUT = 900

# Pruning
MIRROR_THRESHOLD_WR = 0.35
MIRROR_MIN_ROUNDS = 20
MAX_AGENTS = 55

# Ensemble
MAX_ENSEMBLES_PER_CYCLE = 2
ENSEMBLE_MIN_MEMBER_ROUNDS = 20
ENSEMBLE_MIN_MEMBER_WR = 0.48

# Cross-pollination
CROSS_POLLINATE_TOP_N = 3
CROSS_POLLINATE_BOTTOM_N = 3

# Synthesis
SYNTHESIS_MIN_FORUM_SCORE = 2
MAX_SYNTHESIS_PER_CYCLE = 1


# ---------------------------------------------------------------------------
# Phase 1: ANALYZE
# ---------------------------------------------------------------------------
def get_agent_stats(agents_dir: str = AGENTS_DIR) -> list[dict]:
    """Get current stats for all agents with deep metrics."""
    stats = []
    for name in sorted(os.listdir(agents_dir)):
        agent_dir = os.path.join(agents_dir, name)
        if not os.path.isdir(agent_dir) or not name.startswith("agent-"):
            continue
        preds = read_jsonl(os.path.join(agent_dir, "predictions.jsonl"))
        scored = [p for p in preds if p.get("correct") is not None]
        wins = sum(1 for p in scored if p["correct"])
        wr = wins / len(scored) if scored else 0

        config = {}
        cp = os.path.join(agent_dir, "agent_config.json")
        if os.path.exists(cp):
            try:
                with open(cp) as f:
                    config = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        # Recent WR windows for regime detection
        recent_15 = scored[-15:] if len(scored) >= 15 else scored
        recent_30 = scored[-30:] if len(scored) >= 30 else scored
        recent_wr = sum(1 for p in recent_15 if p["correct"]) / len(recent_15) if recent_15 else 0
        recent_30_wr = sum(1 for p in recent_30 if p["correct"]) / len(recent_30) if recent_30 else 0

        # Streak analysis
        streak = 0
        streak_type = None
        for p in reversed(scored):
            if streak_type is None:
                streak_type = p["correct"]
                streak = 1
            elif p["correct"] == streak_type:
                streak += 1
            else:
                break

        # Confidence calibration: avg confidence on wins vs losses
        win_confs = [p.get("confidence", 0.5) for p in scored if p["correct"]]
        loss_confs = [p.get("confidence", 0.5) for p in scored if not p["correct"]]
        avg_win_conf = sum(win_confs) / len(win_confs) if win_confs else 0.5
        avg_loss_conf = sum(loss_confs) / len(loss_confs) if loss_confs else 0.5

        stats.append({
            "name": name,
            "win_rate": wr,
            "recent_wr": recent_wr,
            "recent_30_wr": recent_30_wr,
            "rounds": len(scored),
            "wins": wins,
            "mirror": config.get("mirror", False),
            "model": normalize_model_name(config.get("model"), DEFAULT_PREDICTION_MODEL),
            "is_ensemble": "ensemble" in name,
            "streak": streak,
            "streak_type": "win" if streak_type else "loss" if streak_type is not None else None,
            "avg_win_confidence": avg_win_conf,
            "avg_loss_confidence": avg_loss_conf,
            "regime_delta": wr - recent_wr,  # positive = degrading
        })
    return stats


def analyze_phase(stats: list[dict]) -> dict:
    """Phase 1: Deep analysis of current state."""
    logger.info("Phase 1: ANALYZE")
    total_agents = len(stats)
    proven = [s for s in stats if s["rounds"] >= 20]
    above_50 = [s for s in proven if s["win_rate"] > 0.50]

    logger.info(f"  {total_agents} agents, {len(proven)} with 20+ rounds, {len(above_50)} above 50%")

    analysis = {
        "total_agents": total_agents,
        "proven_count": len(proven),
        "above_50_count": len(above_50),
        "degrading": [],
        "improving": [],
        "hot_streaks": [],
        "cold_streaks": [],
    }

    if proven:
        best = max(proven, key=lambda s: s["win_rate"])
        worst = min(proven, key=lambda s: s["win_rate"])
        best_recent = max(proven, key=lambda s: s["recent_wr"])
        logger.info(f"  Best overall: {best['name']} ({best['win_rate']:.1%}, {best['rounds']}r)")
        logger.info(f"  Best recent:  {best_recent['name']} ({best_recent['recent_wr']:.1%} last 15r)")
        logger.info(f"  Worst proven: {worst['name']} ({worst['win_rate']:.1%}, {worst['rounds']}r)")

        # Regime-shift detection
        degrading = [s for s in proven if s["regime_delta"] > 0.15]
        improving = [s for s in proven if s["regime_delta"] < -0.10]
        analysis["degrading"] = degrading
        analysis["improving"] = improving

        if degrading:
            logger.warning(f"  REGIME SHIFT: {len(degrading)} agents degrading:")
            for s in degrading:
                logger.warning(f"    {s['name']}: overall={s['win_rate']:.0%} recent={s['recent_wr']:.0%}")
        if improving:
            logger.info(f"  IMPROVING: {len(improving)} agents gaining edge:")
            for s in improving:
                logger.info(f"    {s['name']}: overall={s['win_rate']:.0%} recent={s['recent_wr']:.0%}")

        # Streak detection
        hot = [s for s in proven if s["streak"] >= 4 and s["streak_type"] == "win"]
        cold = [s for s in proven if s["streak"] >= 4 and s["streak_type"] == "loss"]
        analysis["hot_streaks"] = hot
        analysis["cold_streaks"] = cold
        if hot:
            hot_str = ", ".join(f"{s['name']} ({s['streak']}W)" for s in hot)
            logger.info(f"  HOT STREAKS: {hot_str}")
        if cold:
            cold_str = ", ".join(f"{s['name']} ({s['streak']}L)" for s in cold)
            logger.warning(f"  COLD STREAKS: {cold_str}")

    return analysis


# ---------------------------------------------------------------------------
# Phase 2: HARVEST — Mine shared knowledge
# ---------------------------------------------------------------------------
def harvest_knowledge() -> dict:
    """Phase 2: Read and synthesize shared knowledge forum."""
    logger.info("Phase 2: HARVEST — mining shared knowledge")
    if not os.path.isdir(SHARED_DIR):
        logger.info("  No shared knowledge directory")
        return {"insights": [], "top_posts": [], "actionable": []}

    ensure_shared_knowledge_forum(SHARED_DIR)
    forum = SharedKnowledgeForum(SHARED_DIR)
    index = forum._read_index()
    posts = index.get("posts", [])

    if not posts:
        logger.info("  No forum posts yet")
        return {"insights": [], "top_posts": [], "actionable": []}

    # Sort by score (most upvoted first)
    ranked = sorted(posts, key=lambda p: (-p.get("score", 0), -p.get("comments_count", 0)))

    top_posts = ranked[:10]
    logger.info(f"  {len(posts)} forum posts, top 5:")
    for p in ranked[:5]:
        logger.info(f"    [{p.get('score', 0):+d}] {p.get('title', '?')[:60]} ({p.get('author', '?')})")

    # Extract actionable discoveries (high score, not yet spawned as agents)
    actionable = [
        p for p in ranked
        if p.get("score", 0) >= SYNTHESIS_MIN_FORUM_SCORE
    ]
    logger.info(f"  {len(actionable)} actionable discoveries (score >= {SYNTHESIS_MIN_FORUM_SCORE})")

    # Read core insight files
    insights = []
    for fname in ["approaches.md", "tournament-insights.md"]:
        fpath = os.path.join(SHARED_DIR, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                insights.append(f.read())

    # Read recent discoveries
    discoveries = sorted([
        f for f in os.listdir(SHARED_DIR) if f.startswith("discovery-")
    ])
    for fname in discoveries[-5:]:
        with open(os.path.join(SHARED_DIR, fname)) as f:
            insights.append(f.read())

    return {
        "insights": insights,
        "top_posts": top_posts,
        "actionable": actionable,
        "insights_text": "\n\n---\n\n".join(insights),
    }


# ---------------------------------------------------------------------------
# Phase 3: BACKTEST — Walk-forward validation
# ---------------------------------------------------------------------------
async def backtest_phase(stats: list[dict], rounds: list[dict]) -> dict:
    """Phase 3: Walk-forward backtest across market regimes."""
    logger.info("Phase 3: BACKTEST")

    if len(rounds) < MIN_ROUNDS_FOR_BACKTEST:
        logger.info(f"  Only {len(rounds)} rounds — need {MIN_ROUNDS_FOR_BACKTEST}, skipping")
        return {"results": [], "walk_forward": [], "regime_scores": {}}

    # Candidates: non-mirror, non-ensemble, with some data
    bt_candidates = [
        s["name"] for s in stats
        if not s["mirror"]
        and not s["is_ensemble"]
        and s["rounds"] >= 5
    ]

    if not bt_candidates:
        logger.info("  No backtest candidates")
        return {"results": [], "walk_forward": [], "regime_scores": {}}

    bt = Backtester(
        agents_dir=AGENTS_DIR,
        data_dir=DATA_DIR,
        model=DEFAULT_PREDICTION_MODEL,
        timeout=90,
        concurrency=8,
        batch_size=10,
    )

    # Simple train/test backtest
    _, test_rounds = split_train_test(rounds, 0.7)
    bt_results = []
    if len(test_rounds) >= 10:
        logger.info(f"  Backtesting {len(bt_candidates)} agents on {len(test_rounds)} test rounds...")
        bt_results = await bt.backtest_all(bt_candidates, test_rounds, agent_concurrency=3)

        for r in sorted(bt_results, key=lambda x: x.get("win_rate", 0), reverse=True)[:5]:
            if "error" not in r:
                logger.info(f"    {r['agent']}: {r['win_rate']:.1%} (test set)")

    # Walk-forward validation for regime robustness
    wf_splits = walk_forward_splits(
        rounds,
        train_size=WALK_FORWARD_TRAIN_SIZE,
        test_size=WALK_FORWARD_TEST_SIZE,
        step=WALK_FORWARD_STEP,
    )

    regime_scores = {}  # agent -> list of test WRs across splits
    if wf_splits and len(bt_candidates) <= 15:
        logger.info(f"  Walk-forward: {len(wf_splits)} splits, {len(bt_candidates)} agents")
        # Only run walk-forward on top candidates to save API costs
        wf_candidates = bt_candidates[:10]
        for split_idx, (train, test) in enumerate(wf_splits):
            if len(test) < 5:
                continue
            split_results = await bt.backtest_all(wf_candidates, test, agent_concurrency=3)
            for r in split_results:
                if "error" in r:
                    continue
                regime_scores.setdefault(r["agent"], []).append(r["win_rate"])

        # Report walk-forward stability
        if regime_scores:
            logger.info("  Walk-forward stability (mean +/- std across splits):")
            stability = []
            for agent, wrs in sorted(regime_scores.items()):
                mean_wr = sum(wrs) / len(wrs)
                if len(wrs) > 1:
                    variance = sum((w - mean_wr) ** 2 for w in wrs) / (len(wrs) - 1)
                    std_wr = variance ** 0.5
                else:
                    std_wr = 0.0
                stability.append((agent, mean_wr, std_wr, len(wrs)))

            stability.sort(key=lambda x: x[1], reverse=True)
            for agent, mean, std, n in stability[:5]:
                logger.info(f"    {agent}: {mean:.1%} +/- {std:.1%} ({n} splits)")

    return {
        "results": bt_results,
        "walk_forward": wf_splits,
        "regime_scores": regime_scores,
    }


# ---------------------------------------------------------------------------
# Phase 4: IDENTIFY — Find evolution targets
# ---------------------------------------------------------------------------
def identify_targets(stats: list[dict], analysis: dict, regime_scores: dict) -> list[dict]:
    """Phase 4: Regime-aware identification of evolution targets."""
    logger.info("Phase 4: IDENTIFY")

    targets = []
    for s in stats:
        if s["mirror"] or s["is_ensemble"]:
            continue
        if s["rounds"] < EVOLVE_MIN_ROUNDS:
            continue

        # Primary criterion: recent performance below threshold
        needs_evolve = s["recent_wr"] < EVOLVE_THRESHOLD_WR

        # Secondary: degrading agents even if overall WR is OK
        if not needs_evolve and s in analysis.get("degrading", []):
            needs_evolve = True

        # Tertiary: walk-forward instability (high std = inconsistent)
        wf_wrs = regime_scores.get(s["name"], [])
        if wf_wrs and len(wf_wrs) >= 3:
            wf_mean = sum(wf_wrs) / len(wf_wrs)
            if wf_mean < EVOLVE_THRESHOLD_WR:
                needs_evolve = True

        if needs_evolve:
            # Priority: worse recent WR = evolve first
            targets.append(s)

    # Sort by recent WR ascending (worst first)
    targets.sort(key=lambda s: s["recent_wr"])

    if targets:
        logger.info(f"  {len(targets)} evolution targets (showing top {MAX_EVOLVE_PER_CYCLE}):")
        for s in targets[:MAX_EVOLVE_PER_CYCLE]:
            logger.info(f"    {s['name']}: recent={s['recent_wr']:.1%} overall={s['win_rate']:.1%} ({s['rounds']}r)")
    else:
        logger.info("  No agents need evolution this cycle")

    return targets[:MAX_EVOLVE_PER_CYCLE]


# ---------------------------------------------------------------------------
# Phase 5: EVOLVE — Knowledge-enriched evolution with backtest validation
# ---------------------------------------------------------------------------
async def evolve_phase(targets: list[dict], knowledge: dict, rounds: list[dict]) -> list[dict]:
    """Phase 5: Evolve underperformers with shared knowledge context."""
    if not targets:
        return []

    logger.info(f"Phase 5: EVOLVE — {len(targets)} agents")
    evolver = StrategyEvolver(AGENTS_DIR, DATA_DIR, timeout_seconds=EVOLVE_TIMEOUT, evaluation_window=5)

    # Inject knowledge insights into agent notes before evolution
    knowledge_text = knowledge.get("insights_text", "")
    top_posts = knowledge.get("top_posts", [])
    if top_posts:
        post_lines = []
        for p in top_posts[:5]:
            post_lines.append(f"- [{p.get('score', 0):+d}] {p.get('title', '?')} (by {p.get('author', '?')})")
        knowledge_text += "\n\n## Top Forum Discoveries\n" + "\n".join(post_lines)

    evolved = []

    async def _evolve_one(s):
        agent_name = s["name"]
        agent_dir = os.path.join(AGENTS_DIR, agent_name)
        logger.info(f"  Evolving {agent_name} (recent={s['recent_wr']:.1%})...")

        # Inject knowledge context into agent notes
        if knowledge_text:
            notes_path = os.path.join(agent_dir, "notes.md")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            suggestion = (
                f"\n## Factory Knowledge Injection ({ts})\n"
                f"The factory has compiled the following insights from shared knowledge.\n"
                f"Consider these when evolving your strategy:\n\n"
                f"{knowledge_text[:2000]}\n"
            )
            existing = ""
            if os.path.exists(notes_path):
                with open(notes_path) as f:
                    existing = f.read()
            with open(notes_path, "w") as f:
                f.write(existing + suggestion)

        result = await evolver.evolve_agent(agent_name)
        if not result:
            logger.warning(f"    Evolution failed for {agent_name}")
            return None

        # Backup before applying
        strategy_path = os.path.join(agent_dir, "strategy.md")
        prev_path = os.path.join(agent_dir, "strategy.md.prev")
        if os.path.exists(strategy_path):
            shutil.copy2(strategy_path, prev_path)

        evolver.apply_evolution(agent_name, result)
        change = result.get("change_description", "unknown")
        logger.info(f"    Evolved {agent_name}: {change[:80]}")

        # Quick backtest validation on test set if we have enough rounds
        if len(rounds) >= MIN_ROUNDS_FOR_BACKTEST:
            train_rounds, test_rounds = split_train_test(rounds, 0.7)
            if len(test_rounds) >= 10:
                bt = Backtester(
                    agents_dir=AGENTS_DIR,
                    data_dir=DATA_DIR,
                    model=DEFAULT_PREDICTION_MODEL,
                    timeout=90,
                    concurrency=8,
                    batch_size=10,
                )
                bt_result = await bt.backtest_agent(agent_name, test_rounds)
                if "error" not in bt_result:
                    bt_wr = bt_result["win_rate"]
                    logger.info(f"    Post-evolution backtest: {bt_wr:.1%} ({bt_result['wins']}/{bt_result['total']})")

                    # Revert if clearly worse
                    if bt_wr < 0.35:
                        logger.warning(f"    REVERT: backtest {bt_wr:.1%} < 35%, reverting")
                        if os.path.exists(prev_path):
                            shutil.copy2(prev_path, strategy_path)
                        return None

        return {"agent": agent_name, "change": change}

    results = await asyncio.gather(
        *[_evolve_one(s) for s in targets],
        return_exceptions=True,
    )

    for r in results:
        if isinstance(r, Exception):
            logger.error(f"    Evolution error: {r}")
        elif r:
            evolved.append(r)

    return evolved


# ---------------------------------------------------------------------------
# Phase 6: CROSS-POLLINATE — Transfer winning traits
# ---------------------------------------------------------------------------
def cross_pollinate_phase(stats: list[dict]):
    """Phase 6: Transfer scripts and insights from top performers to underperformers."""
    logger.info("Phase 6: CROSS-POLLINATE")

    proven = [s for s in stats if s["rounds"] >= 20 and not s["mirror"] and not s["is_ensemble"]]
    if len(proven) < 4:
        logger.info("  Not enough proven agents for cross-pollination")
        return

    top = sorted(proven, key=lambda s: s["recent_wr"], reverse=True)[:CROSS_POLLINATE_TOP_N]
    bottom = sorted(proven, key=lambda s: s["recent_wr"])[:CROSS_POLLINATE_BOTTOM_N]

    # Don't cross-pollinate with yourself
    bottom = [s for s in bottom if s["name"] not in [t["name"] for t in top]]
    if not bottom:
        logger.info("  No valid cross-pollination pairs")
        return

    pollinator = CrossPollinator(AGENTS_DIR)

    for top_agent in top:
        top_scripts_dir = os.path.join(AGENTS_DIR, top_agent["name"], "scripts")
        if not os.path.isdir(top_scripts_dir):
            continue
        scripts = [f for f in os.listdir(top_scripts_dir) if f.endswith(".py")]
        if not scripts:
            continue

        for bottom_agent in bottom:
            # Only add suggestion, don't forcefully copy scripts
            # Let the evolution process integrate the insight naturally
            suggestion = (
                f"Top-performing agent `{top_agent['name']}` "
                f"(recent WR: {top_agent['recent_wr']:.0%}) uses scripts: "
                f"{', '.join(scripts)}. "
                f"Consider studying their approach for ideas to improve your "
                f"strategy (your recent WR: {bottom_agent['recent_wr']:.0%})."
            )
            pollinator.add_suggestion(bottom_agent["name"], suggestion)
            logger.info(f"  Suggested {top_agent['name']}'s approach to {bottom_agent['name']}")


# ---------------------------------------------------------------------------
# Phase 7: SYNTHESIZE — Spawn new agents from top discoveries
# ---------------------------------------------------------------------------
def synthesize_phase(knowledge: dict, stats: list[dict]) -> list[str]:
    """Phase 7: Spawn new strategy agents from high-scoring forum discoveries."""
    logger.info("Phase 7: SYNTHESIZE")

    total_agents = len(stats)
    if total_agents >= MAX_AGENTS:
        logger.info(f"  Agent pool full ({total_agents}/{MAX_AGENTS}), skipping synthesis")
        return []

    actionable = knowledge.get("actionable", [])
    if not actionable:
        logger.info("  No actionable discoveries to synthesize")
        return []

    # Check which discoveries have already been spawned
    existing_agents = {s["name"] for s in stats}
    spawned = []
    spawner = AgentSpawner(AGENTS_DIR)

    for post in actionable[:MAX_SYNTHESIS_PER_CYCLE]:
        title = post.get("title", "unknown")
        body = ""
        post_path = post.get("post_path", "")
        if post_path and os.path.exists(post_path):
            with open(post_path) as f:
                body = f.read()

        if not body or len(body) < 20:
            continue

        # Check if already spawned (rough heuristic: any agent with similar name)
        title_slug = title.lower().replace(" ", "-")[:30]
        already_exists = any(title_slug[:15] in name.lower() for name in existing_agents)
        if already_exists:
            continue

        # Create a seed strategy from the discovery
        strategy = (
            f"# Strategy from Shared Discovery: {title}\n\n"
            f"## Origin\n"
            f"This strategy was synthesized by the Strategy Factory from a high-scoring\n"
            f"shared knowledge discovery (score: {post.get('score', 0)}).\n\n"
            f"## Core Insight\n{body}\n\n"
            f"## Decision Logic\n"
            f"Apply the insight above to predict BTC 5-minute direction.\n"
            f"Use the market snapshot data (candles, order book, derivatives) to\n"
            f"implement the described pattern.\n\n"
            f"## Data Sources\n"
            f"- `binance_candles_5m` — price and volume candles\n"
            f"- `polymarket_orderbook` — market consensus\n"
            f"- `binance_orderbook` — exchange order flow\n"
            f"- `polling/funding_rate`, `polling/taker_volume` — derivatives data\n\n"
            f"## Confidence\n"
            f"- Strong signal match: 70%\n"
            f"- Moderate signal: 55%\n"
            f"- Weak/ambiguous: 50%\n"
        )

        seed = {
            "name": f"synthesis-{title_slug[:20]}",
            "strategy": strategy,
        }
        try:
            agent_name = spawner.spawn_from_seed(seed)
            spawned.append(agent_name)
            logger.info(f"  Synthesized: {agent_name} from discovery '{title[:50]}'")
        except Exception as e:
            logger.error(f"  Synthesis failed for '{title[:50]}': {e}")

    if not spawned:
        logger.info("  No new agents synthesized this cycle")

    return spawned


# ---------------------------------------------------------------------------
# Phase 8: ENSEMBLE — Rebuild from current top performers
# ---------------------------------------------------------------------------
def ensemble_phase(stats: list[dict]) -> list[str]:
    """Phase 8: Create/refresh ensembles from current best agents."""
    logger.info("Phase 8: ENSEMBLE")

    total_agents = len(stats)
    if total_agents >= MAX_AGENTS:
        logger.info(f"  Agent pool full ({total_agents}/{MAX_AGENTS}), skipping ensemble")
        return []

    # Find eligible members: proven, decent WR, not mirrors/ensembles
    eligible = [
        s for s in stats
        if s["rounds"] >= ENSEMBLE_MIN_MEMBER_ROUNDS
        and s["recent_wr"] >= ENSEMBLE_MIN_MEMBER_WR
        and not s["mirror"]
        and not s["is_ensemble"]
    ]

    if len(eligible) < 3:
        logger.info(f"  Only {len(eligible)} eligible members, need 3+")
        return []

    # Sort by recent WR for best current performers
    eligible.sort(key=lambda s: s["recent_wr"], reverse=True)
    logger.info(f"  {len(eligible)} eligible ensemble members")

    created = []
    existing_ensembles = [s["name"] for s in stats if s["is_ensemble"]]

    # Create top-3 recent ensemble if we don't have too many already
    if len(existing_ensembles) < 8 and len(eligible) >= 3:
        top3 = [s["name"] for s in eligible[:3]]
        ts = time.strftime("%m%d")
        ensemble_name = f"top3-recent-{ts}"

        # Check we don't already have this exact combination
        combo_key = "-".join(sorted(top3))
        already_exists = False
        for ens_name in existing_ensembles:
            ens_dir = os.path.join(AGENTS_DIR, ens_name)
            members_file = os.path.join(ens_dir, "ensemble_members.json")
            if os.path.exists(members_file):
                try:
                    with open(members_file) as f:
                        members = json.load(f).get("members", [])
                    if "-".join(sorted(members)) == combo_key:
                        already_exists = True
                        break
                except (json.JSONDecodeError, OSError):
                    pass

        if not already_exists:
            try:
                name = create_ensemble_agent(
                    AGENTS_DIR,
                    ensemble_name,
                    top3,
                    weighting="win-rate",
                    description=f"Auto-created by Strategy Factory. Members: {', '.join(top3)}",
                )
                created.append(name)
                logger.info(f"  Created ensemble: {name}")
            except Exception as e:
                logger.error(f"  Ensemble creation failed: {e}")

    if not created:
        logger.info("  No new ensembles needed")

    return created


# ---------------------------------------------------------------------------
# Phase 9: PRUNE — Mirror anti-predictive, retire hopeless
# ---------------------------------------------------------------------------
def prune_phase(stats: list[dict]) -> dict:
    """Phase 9: Mirror anti-predictive agents and retire hopeless ones."""
    logger.info("Phase 9: PRUNE")

    spawner = AgentSpawner(AGENTS_DIR)
    total_agents = len(stats)
    mirrors_created = []
    retired = []

    for s in stats:
        if s["mirror"] or s["is_ensemble"]:
            continue
        if s["rounds"] < MIRROR_MIN_ROUNDS:
            continue

        # Auto-mirror extremely anti-predictive agents
        if s["win_rate"] < MIRROR_THRESHOLD_WR:
            existing_mirrors = [
                a["name"] for a in stats
                if a["mirror"] and s["name"].split("-", 2)[-1] in a["name"]
            ]
            if not existing_mirrors and total_agents < MAX_AGENTS:
                try:
                    mirror = spawner.spawn_mirror(s["name"])
                    mirrors_created.append(mirror)
                    total_agents += 1
                    logger.info(f"  Mirror: {s['name']} ({s['win_rate']:.1%}) -> {mirror}")
                except Exception as e:
                    logger.error(f"  Mirror failed for {s['name']}: {e}")

    logger.info(f"  {total_agents} agents after pruning, {len(mirrors_created)} mirrors created")
    return {"mirrors": mirrors_created, "retired": retired, "total": total_agents}


# ---------------------------------------------------------------------------
# Phase 10: PUBLISH — Post findings back to shared knowledge
# ---------------------------------------------------------------------------
def publish_phase(
    stats: list[dict],
    analysis: dict,
    bt_results: list[dict],
    regime_scores: dict,
    evolved: list[dict],
    rounds: list[dict],
):
    """Phase 10: Publish cycle findings to shared knowledge."""
    logger.info("Phase 10: PUBLISH")

    ensure_shared_knowledge_forum(SHARED_DIR)

    # 1. Leaderboard snapshot (same as before, but less frequent)
    ts = time.strftime("%Y%m%d-%H%M")
    snapshot_path = os.path.join(SHARED_DIR, f"leaderboard-snapshot-{ts}.md")
    if not os.path.exists(snapshot_path):
        proven = sorted(
            [s for s in stats if s["rounds"] >= 20],
            key=lambda s: s["win_rate"],
            reverse=True,
        )
        if proven:
            lines = [f"# Leaderboard Snapshot -- {ts} ({len(rounds)} rounds)\n"]
            for s in proven:
                tier = (
                    "T1" if s["win_rate"] > 0.55
                    else "T2" if s["win_rate"] > 0.50
                    else "T3" if s["win_rate"] > 0.45
                    else "T4"
                )
                recent_tag = ""
                if s["regime_delta"] > 0.10:
                    recent_tag = " [DEGRADING]"
                elif s["regime_delta"] < -0.10:
                    recent_tag = " [IMPROVING]"
                lines.append(
                    f"- [{tier}] {s['name']}: {s['win_rate']:.1%} "
                    f"(recent: {s['recent_wr']:.1%}, {s['rounds']}r){recent_tag}"
                )
            with open(snapshot_path, "w") as f:
                f.write("\n".join(lines))
            logger.info(f"  Leaderboard snapshot: {snapshot_path}")

    # 2. Walk-forward stability report
    if regime_scores:
        stability_path = os.path.join(SHARED_DIR, f"walk-forward-stability-{ts}.md")
        if not os.path.exists(stability_path):
            lines = [f"# Walk-Forward Stability Report -- {ts}\n"]
            lines.append("Agents tested across multiple time windows for regime robustness:\n")
            for agent, wrs in sorted(regime_scores.items(), key=lambda x: sum(x[1]) / len(x[1]), reverse=True):
                mean = sum(wrs) / len(wrs)
                if len(wrs) > 1:
                    std = (sum((w - mean) ** 2 for w in wrs) / (len(wrs) - 1)) ** 0.5
                else:
                    std = 0.0
                lines.append(f"- {agent}: {mean:.1%} +/- {std:.1%} ({len(wrs)} windows)")
            with open(stability_path, "w") as f:
                f.write("\n".join(lines))

    # 3. Post regime shift warnings to forum
    forum = SharedKnowledgeForum(SHARED_DIR)
    degrading = analysis.get("degrading", [])
    if len(degrading) >= 3:
        warning_title = f"Regime Shift Warning: {len(degrading)} agents degrading ({ts})"
        warning_body = (
            f"Factory detected {len(degrading)} agents with significant performance degradation "
            f"(overall WR >> recent 15-round WR):\n\n"
        )
        for s in degrading:
            warning_body += f"- {s['name']}: overall {s['win_rate']:.0%} -> recent {s['recent_wr']:.0%}\n"
        warning_body += (
            f"\nThis suggests a market regime change. Agents should consider "
            f"adapting strategies to current conditions."
        )
        forum.create_post("strategy-factory", warning_title, warning_body)
        logger.info(f"  Posted regime shift warning to forum")

    # 4. Post evolution outcomes
    if evolved:
        for ev in evolved:
            change = ev.get("change", "unknown")
            agent = ev.get("agent", "unknown")
            # Don't flood forum — only post if the change is interesting
            if len(change) > 20:
                forum.create_post(
                    "strategy-factory",
                    f"Evolution: {agent} strategy update",
                    f"The factory evolved {agent}'s strategy:\n\n{change[:500]}",
                )

    logger.info("  Publishing complete")


# ---------------------------------------------------------------------------
# CORRELATE — Compare backtest vs live
# ---------------------------------------------------------------------------
def correlate_phase(stats: list[dict], bt_results: list[dict]):
    """Compare backtest vs live win rates."""
    if not bt_results:
        return

    logger.info("  CORRELATE: backtest vs live accuracy")
    bt_map = {r["agent"]: r["win_rate"] for r in bt_results if "error" not in r}
    live_map = {s["name"]: s["win_rate"] for s in stats if s["rounds"] >= 20}
    correlations = []
    for name in bt_map:
        if name in live_map:
            correlations.append((name, live_map[name], bt_map[name], bt_map[name] - live_map[name]))
    if correlations:
        correlations.sort(key=lambda x: abs(x[3]), reverse=True)
        avg_gap = sum(abs(c[3]) for c in correlations) / len(correlations)
        logger.info(f"  Avg |backtest - live| gap: {avg_gap:.1%} ({len(correlations)} agents)")
        for name, live_wr, bt_wr, gap in correlations[:5]:
            logger.info(f"    {name}: live={live_wr:.0%} bt={bt_wr:.0%} gap={gap:+.0%}")


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
async def run_factory_cycle(cycle_num: int) -> bool:
    """Run one complete factory optimization cycle. Returns True if changes were made."""
    logger.info(f"\n{'='*70}")
    logger.info(f"  STRATEGY FACTORY v2 — CYCLE #{cycle_num}")
    logger.info(f"  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*70}")

    cycle_start = time.time()
    changes_made = False

    # Phase 0: DECISION QUALITY — analyze all agents' intraround decision patterns
    logger.info("Phase 0: DECISION QUALITY — analyzing revision patterns")
    decision_profiles = analyze_all_agents(AGENTS_DIR, DATA_DIR)
    if decision_profiles:
        insights = generate_decision_insights(decision_profiles)
        if insights:
            insights_path = os.path.join(SHARED_DIR, "decision-quality-insights.md")
            with open(insights_path, "w") as f:
                f.write(insights)
            logger.info(f"  Analyzed {len(decision_profiles)} agents, insights written")

    # Phase 0b: OUTCOME PATTERNS — analyze outcome sequences for autocorrelation
    logger.info("Phase 0b: OUTCOME PATTERNS — analyzing outcome sequences")
    try:
        outcome_analysis = run_full_analysis(DATA_DIR)
        if outcome_analysis.get("total_rounds", 0) > 0:
            outcome_context = build_outcome_context(DATA_DIR)
            if outcome_context:
                outcome_path = os.path.join(SHARED_DIR, "outcome-patterns.md")
                with open(outcome_path, "w") as f:
                    f.write(outcome_context)
                logger.info(f"  {outcome_analysis['total_rounds']} rounds analyzed for outcome patterns")
    except Exception as e:
        logger.error(f"  Outcome analysis failed: {e}")

    # Phase 0c: AGENT CORRELATION — compute pairwise prediction correlation
    logger.info("Phase 0c: AGENT CORRELATION — computing prediction diversity")
    try:
        corr_analysis = run_correlation_analysis(AGENTS_DIR)
        if corr_analysis.get("agent_count", 0) > 1:
            corr_context = build_correlation_context(AGENTS_DIR)
            if corr_context:
                corr_path = os.path.join(SHARED_DIR, "agent-correlations.md")
                with open(corr_path, "w") as f:
                    f.write(corr_context)
                logger.info(f"  Correlation matrix computed for {corr_analysis['agent_count']} agents")
                if corr_analysis.get("recommended_ensemble"):
                    logger.info(f"  Recommended diverse ensemble: {corr_analysis['recommended_ensemble']}")
    except Exception as e:
        logger.error(f"  Correlation analysis failed: {e}")

    # Phase 1: ANALYZE
    stats = get_agent_stats()
    analysis = analyze_phase(stats)

    # Phase 2: HARVEST shared knowledge
    knowledge = harvest_knowledge()

    # Phase 3: BACKTEST with walk-forward validation
    rounds = load_historical_rounds(DATA_DIR)
    bt_data = await backtest_phase(stats, rounds)

    # Phase 4: IDENTIFY evolution targets
    targets = identify_targets(stats, analysis, bt_data.get("regime_scores", {}))

    # Phase 5: EVOLVE with knowledge enrichment
    evolved = await evolve_phase(targets, knowledge, rounds)
    if evolved:
        changes_made = True

    # Phase 6: CROSS-POLLINATE
    cross_pollinate_phase(stats)

    # Phase 7: SYNTHESIZE new agents from discoveries
    synthesized = synthesize_phase(knowledge, stats)
    if synthesized:
        changes_made = True

    # Phase 8: ENSEMBLE refresh
    ensembles = ensemble_phase(stats)
    if ensembles:
        changes_made = True

    # Phase 9: PRUNE
    prune_result = prune_phase(stats)
    if prune_result.get("mirrors") or prune_result.get("retired"):
        changes_made = True

    # Correlate backtest vs live
    correlate_phase(stats, bt_data.get("results", []))

    # Phase 10: PUBLISH findings
    publish_phase(stats, analysis, bt_data.get("results", []), bt_data.get("regime_scores", {}), evolved, rounds)

    elapsed = time.time() - cycle_start
    logger.info(f"\n  Cycle #{cycle_num} complete in {elapsed:.0f}s. Changes: {'YES' if changes_made else 'no'}")

    return changes_made


async def main():
    logger.info("Strategy Factory v2 starting — continuous optimization loop")
    logger.info(f"  Project: {PROJECT_DIR}")
    logger.info(f"  Normal interval: {CYCLE_INTERVAL_SECONDS}s")
    logger.info(f"  Accelerated interval: {ACCELERATED_INTERVAL}s")

    cycle = 1
    while True:
        try:
            changes_made = await run_factory_cycle(cycle)
        except Exception as e:
            logger.error(f"Cycle #{cycle} failed: {e}", exc_info=True)
            changes_made = False

        # Accelerate next cycle if changes were made (feedback loop tightens)
        interval = ACCELERATED_INTERVAL if changes_made else CYCLE_INTERVAL_SECONDS
        cycle += 1
        logger.info(f"Next cycle in {interval}s ({'accelerated' if changes_made else 'normal'})...")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
