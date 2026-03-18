# src/runner/backtester.py
"""
Backtesting engine — replay historical rounds through agents for rapid evaluation.

Instead of waiting 5 minutes per round in real-time, backtesting replays
stored snapshots through agent strategies and compares predictions to known outcomes.

Key design decisions:
- Uses the same Predictor + prompt pipeline as live trading (no shortcut)
- Supports train/test split to detect overfitting
- Runs agents in parallel across rounds for maximum speed
- Records results per-agent with confidence intervals
- Does NOT modify live prediction files — writes to separate backtest output
"""
import asyncio
import json
import logging
import math
import os
import time

from src.runner.predictor import Predictor, parse_prediction_response
from src.io_utils import read_jsonl

logger = logging.getLogger(__name__)


def load_historical_rounds(data_dir: str) -> list[dict]:
    """Load all historical rounds with snapshots and outcomes."""
    rounds_dir = os.path.join(data_dir, "rounds")
    if not os.path.isdir(rounds_dir):
        return []

    rounds = []
    for dirname in sorted(os.listdir(rounds_dir)):
        if not dirname.isdigit():
            continue
        round_dir = os.path.join(rounds_dir, dirname)
        snapshot_path = os.path.join(round_dir, "snapshot.json")
        result_path = os.path.join(round_dir, "result.json")

        if not os.path.exists(snapshot_path) or not os.path.exists(result_path):
            continue

        with open(result_path) as f:
            result = json.load(f)
        outcome = result.get("outcome")
        if not outcome:
            continue

        rounds.append({
            "timestamp": int(dirname),
            "snapshot_path": snapshot_path,
            "outcome": outcome,
        })

    return rounds


def split_train_test(rounds: list[dict], train_ratio: float = 0.7) -> tuple[list[dict], list[dict]]:
    """Split rounds into train and test sets (chronological, no shuffling)."""
    split_idx = int(len(rounds) * train_ratio)
    return rounds[:split_idx], rounds[split_idx:]


def walk_forward_splits(rounds: list[dict], train_size: int = 40, test_size: int = 15, step: int = 10) -> list[tuple[list[dict], list[dict]]]:
    """Generate walk-forward validation splits.

    Slides a window through the data:
      Split 1: train=[0..39], test=[40..54]
      Split 2: train=[10..49], test=[50..64]
      Split 3: train=[20..59], test=[60..74]
      ...

    This tests whether the strategy works across different market regimes,
    not just one specific period.

    Returns list of (train, test) tuples.
    """
    splits = []
    i = 0
    while i + train_size + test_size <= len(rounds):
        train = rounds[i:i + train_size]
        test = rounds[i + train_size:i + train_size + test_size]
        splits.append((train, test))
        i += step
    return splits


def trim_snapshot(snapshot: dict) -> dict:
    """Trim snapshot to reduce token usage (same as live system)."""
    if "binance_candles_5m" in snapshot:
        candles = snapshot["binance_candles_5m"].get("candles", [])
        snapshot["binance_candles_5m"]["candles"] = candles[-20:]
    if "binance_trades_recent" in snapshot:
        trades = snapshot["binance_trades_recent"].get("trades", [])
        snapshot["binance_trades_recent"]["trades"] = trades[-50:]
    if "polling" in snapshot and isinstance(snapshot["polling"], dict):
        for key, val in snapshot["polling"].items():
            if isinstance(val, dict) and "data" in val:
                val["data"] = val["data"][-5:]
    return snapshot


def wilson_confidence_interval(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for win rate confidence bounds."""
    if total == 0:
        return 0.0, 1.0
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return max(0.0, center - spread), min(1.0, center + spread)


class Backtester:
    def __init__(
        self,
        agents_dir: str,
        data_dir: str,
        model: str = "haiku",
        timeout: int = 90,
        concurrency: int = 10,
        batch_size: int = 10,
    ):
        self.agents_dir = agents_dir
        self.data_dir = data_dir
        self.predictor = Predictor(timeout_seconds=timeout, model=model)
        self.concurrency = concurrency
        self.batch_size = batch_size

    async def backtest_agent(
        self,
        agent_name: str,
        rounds: list[dict],
        batch_size: int = 10,
    ) -> dict:
        """Run a single agent through historical rounds using batch prediction.

        batch_size: number of rounds per Claude call. Higher = faster but less accurate
        (the model has to process more data per call). 10 is a good balance.
        Set to 1 for single-round mode (slower but matches live behavior exactly).
        """
        agent_dir = os.path.join(self.agents_dir, agent_name)
        strategy_path = os.path.join(agent_dir, "strategy.md")
        if not os.path.exists(strategy_path):
            return {"error": f"No strategy.md for {agent_name}"}

        with open(strategy_path) as f:
            strategy = f.read()

        # Read agent config for mirror/model override
        agent_config = {}
        config_path = os.path.join(agent_dir, "agent_config.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                agent_config = json.load(f)

        scripts = {}
        scripts_dir = os.path.join(agent_dir, "scripts")
        if os.path.isdir(scripts_dir):
            for fname in os.listdir(scripts_dir):
                fpath = os.path.join(scripts_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        scripts[fname] = f.read()

        model = agent_config.get("model")
        predictions = []

        if batch_size > 1:
            # BATCH MODE: send multiple snapshots per Claude call (much faster)
            batches = [rounds[i:i+batch_size] for i in range(0, len(rounds), batch_size)]
            sem = asyncio.Semaphore(self.concurrency)

            async def _predict_batch(batch: list[dict]):
                async with sem:
                    snapshots = []
                    for rd in batch:
                        with open(rd["snapshot_path"]) as f:
                            snapshots.append(trim_snapshot(json.load(f)))

                    results = await self.predictor.get_batch_predictions(
                        agent_dir=agent_dir,
                        strategy=strategy,
                        snapshots=snapshots,
                        scripts=scripts,
                        notes="",
                        model=model,
                    )
                    return list(zip(results, batch))

            tasks = [_predict_batch(batch) for batch in batches]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for br in batch_results:
                if isinstance(br, Exception):
                    logger.warning(f"Batch prediction error: {br}")
                    continue
                for result, rd in br:
                    if result is None:
                        continue
                    prediction = result["prediction"]
                    if agent_config.get("mirror"):
                        prediction = "Down" if prediction == "Up" else "Up"
                    correct = prediction.strip().lower() == rd["outcome"].strip().lower()
                    predictions.append({
                        "round": rd["timestamp"],
                        "prediction": prediction,
                        "outcome": rd["outcome"],
                        "correct": correct,
                        "confidence": result.get("confidence", 0.5),
                        "reasoning": result.get("reasoning", "")[:100],
                    })
        else:
            # SINGLE MODE: one Claude call per round (slower, matches live exactly)
            sem = asyncio.Semaphore(self.concurrency)

            async def _predict_single(round_data: dict):
                async with sem:
                    with open(round_data["snapshot_path"]) as f:
                        snapshot = trim_snapshot(json.load(f))
                    result = await self.predictor.get_prediction(
                        agent_dir=agent_dir,
                        strategy=strategy,
                        snapshot=snapshot,
                        scripts=scripts,
                        recent_results="",
                        notes="",
                        model=model,
                    )
                    return result, round_data

            tasks = [_predict_single(rd) for rd in rounds]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for br in results:
                if isinstance(br, Exception):
                    continue
                result, rd = br
                if result is None:
                    continue
                prediction = result["prediction"]
                if agent_config.get("mirror"):
                    prediction = "Down" if prediction == "Up" else "Up"
                correct = prediction.strip().lower() == rd["outcome"].strip().lower()
                predictions.append({
                    "round": rd["timestamp"],
                    "prediction": prediction,
                    "outcome": rd["outcome"],
                    "correct": correct,
                    "confidence": result.get("confidence", 0.5),
                    "reasoning": result.get("reasoning", "")[:100],
                })

        wins = sum(1 for p in predictions if p["correct"])
        total = len(predictions)
        win_rate = wins / total if total > 0 else 0.0
        ci_low, ci_high = wilson_confidence_interval(wins, total)

        return {
            "agent": agent_name,
            "win_rate": win_rate,
            "wins": wins,
            "total": total,
            "confidence_interval": (ci_low, ci_high),
            "predictions": predictions,
            "mirror": agent_config.get("mirror", False),
            "model": agent_config.get("model", "haiku"),
        }

    async def backtest_all(
        self,
        agent_names: list[str],
        rounds: list[dict],
        agent_concurrency: int = 3,
    ) -> list[dict]:
        """Run multiple agents through historical rounds.

        agent_concurrency controls how many agents run in parallel
        (each agent's rounds are already parallelized internally).
        """
        sem = asyncio.Semaphore(agent_concurrency)
        results = []

        async def _run_agent(name):
            async with sem:
                logger.info(f"Backtesting {name} ({len(rounds)} rounds, batch={self.batch_size})...")
                start = time.time()
                result = await self.backtest_agent(name, rounds, batch_size=self.batch_size)
                elapsed = time.time() - start
                if "error" not in result:
                    logger.info(
                        f"  {name}: {result['win_rate']:.1%} "
                        f"({result['wins']}/{result['total']}) "
                        f"CI [{result['confidence_interval'][0]:.1%}, {result['confidence_interval'][1]:.1%}] "
                        f"in {elapsed:.1f}s"
                    )
                return result

        tasks = [_run_agent(name) for name in agent_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if not isinstance(r, Exception)]

    def save_results(self, results: list[dict], output_path: str, label: str = "backtest"):
        """Save backtest results to JSON."""
        summary = {
            "label": label,
            "timestamp": int(time.time()),
            "agents": [],
        }
        for r in results:
            if "error" in r:
                continue
            summary["agents"].append({
                "agent": r["agent"],
                "win_rate": r["win_rate"],
                "wins": r["wins"],
                "total": r["total"],
                "ci_low": r["confidence_interval"][0],
                "ci_high": r["confidence_interval"][1],
                "mirror": r.get("mirror", False),
                "model": r.get("model", "haiku"),
            })

        summary["agents"].sort(key=lambda x: x["win_rate"], reverse=True)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"Results saved to {output_path}")

    def print_results(self, results: list[dict], label: str = ""):
        """Pretty-print backtest results."""
        valid = [r for r in results if "error" not in r]
        valid.sort(key=lambda x: x["win_rate"], reverse=True)

        print(f"\n{'=' * 80}")
        print(f"  BACKTEST RESULTS{f' — {label}' if label else ''}")
        print(f"  {len(valid)} agents, {valid[0]['total'] if valid else 0} rounds")
        print(f"{'=' * 80}")
        print(f"{'Agent':<50} {'WR':>6} {'W/T':>7} {'CI 95%':>14} {'Model':>7} {'Mirror':>7}")
        print(f"{'-' * 50} {'-' * 6} {'-' * 7} {'-' * 14} {'-' * 7} {'-' * 7}")

        for r in valid:
            ci = f"[{r['confidence_interval'][0]:.0%},{r['confidence_interval'][1]:.0%}]"
            mirror = "YES" if r.get("mirror") else ""
            model = r.get("model", "haiku")
            print(
                f"{r['agent']:<50} {r['win_rate']:>5.1%} "
                f"{r['wins']:>3}/{r['total']:<3} "
                f"{ci:>14} {model:>7} {mirror:>7}"
            )

        # Highlight findings
        if valid:
            best = valid[0]
            worst = valid[-1]
            print(f"\n  Best:  {best['agent']} — {best['win_rate']:.1%}")
            print(f"  Worst: {worst['agent']} — {worst['win_rate']:.1%}")

            # Check mirror vs source
            mirrors = [r for r in valid if r.get("mirror")]
            for m in mirrors:
                source_name = None
                config_path = os.path.join(self.agents_dir, m["agent"], "agent_config.json")
                if os.path.exists(config_path):
                    with open(config_path) as f:
                        source_name = json.load(f).get("source_agent")
                if source_name:
                    source = next((r for r in valid if r["agent"] == source_name), None)
                    if source:
                        diff = m["win_rate"] - source["win_rate"]
                        print(f"\n  Mirror test: {m['agent']}")
                        print(f"    Source {source['agent']}: {source['win_rate']:.1%}")
                        print(f"    Mirror: {m['win_rate']:.1%} (delta: {diff:+.1%})")
