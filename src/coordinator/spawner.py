# src/coordinator/spawner.py
import os
import shutil
import logging

logger = logging.getLogger(__name__)

SEED_STRATEGIES = [
    {
        "name": "orderbook-specialist",
        "strategy": """# Order Book Specialist\n\n## Focus\nAnalyze Polymarket order book to predict BTC 5-minute direction.\n\n## Data Sources\n- Primary: `polymarket_orderbook` (bids/asks depth and imbalance)\n- Secondary: `binance_orderbook` (cross-reference exchange book)\n\n## Decision Logic\n1. Calculate bid/ask imbalance ratio: total_bid_volume / total_ask_volume\n2. If imbalance > 1.5 (heavy buying pressure) -> predict UP\n3. If imbalance < 0.67 (heavy selling pressure) -> predict DOWN\n4. If neutral (0.67-1.5) -> look at which side has more levels near the spread\n5. Check if large orders appeared recently (> 2x average size)\n\n## Confidence\n- Strong imbalance (>2.0 or <0.5): 80%\n- Moderate imbalance: 60%\n- Neutral: 50%\n""",
    },
    {
        "name": "momentum-trader",
        "strategy": """# Momentum Trader\n\n## Focus\nUse Binance 5-minute candle patterns to predict next candle direction.\n\n## Data Sources\n- Primary: `binance_candles_5m` (last 100 candles OHLCV)\n- Secondary: `binance_orderbook`\n\n## Decision Logic\n1. Look at last 3 candles for trend direction\n2. Calculate simple momentum: (close[-1] - close[-3]) / close[-3]\n3. If last 3 candles all green AND volume increasing -> predict UP\n4. If last 3 candles all red AND volume increasing -> predict DOWN\n5. If mixed signals -> look at volume-weighted average price vs current\n6. Check for reversal patterns: long wicks, doji candles\n\n## Confidence\n- Strong 3-candle trend with volume: 75%\n- Weak trend or mixed: 55%\n- Reversal signal detected: 60% (reverse direction)\n""",
    },
    {
        "name": "derivatives-analyst",
        "strategy": """# Derivatives Analyst\n\n## Focus\nUse futures market data to predict spot BTC direction.\n\n## Data Sources\n- Primary: `polling/open_interest`, `polling/funding_rate`\n- Secondary: `polling/long_short_ratio`, `polling/taker_volume`, `polling/top_trader_ratio`\n- Tertiary: `binance_candles_5m`\n\n## Decision Logic\n1. Check funding rate: extreme positive -> overleveraged longs -> potential DOWN\n2. Check funding rate: extreme negative -> overleveraged shorts -> potential UP\n3. Open interest rising + price rising -> strong UP trend continuation\n4. Open interest rising + price falling -> more shorts opening -> potential UP squeeze\n5. Taker buy/sell ratio > 1.3 -> aggressive buying -> UP\n6. Top trader long ratio > 0.6 -> smart money bullish -> UP\n\n## Handling Missing Data\nIf futures data unavailable (451 error), fall back to spot-only analysis.\n\n## Confidence\n- Multiple signals aligned: 75%\n- Single strong signal: 60%\n- Conflicting signals: 50%\n""",
    },
    {
        "name": "contrarian",
        "strategy": """# Contrarian\n\n## Focus\nFade the Polymarket consensus when odds are extreme.\n\n## Data Sources\n- Primary: `polymarket_orderbook` (current implied probability from midpoint)\n- Secondary: `binance_candles_5m`\n\n## Decision Logic\n1. Read Polymarket midpoint price as implied probability\n2. If Up probability > 0.70 -> market very confident in UP -> predict DOWN\n3. If Down probability > 0.70 -> market very confident in DOWN -> predict UP\n4. If probabilities near 50/50 -> skip contrarian, use candle momentum instead\n5. Key insight: extreme confidence in 5-min markets is often wrong because BTC noise dominates\n\n## Confidence\n- Extreme consensus (>75%): 65% (contrarian bet)\n- Moderate consensus (60-75%): 55%\n- Near 50/50: 50% (switch to momentum)\n""",
    },
    {
        "name": "multi-signal-synthesizer",
        "strategy": """# Multi-Signal Synthesizer\n\n## Focus\nCombine all available data sources with weighted voting.\n\n## Data Sources\nALL available sources, weighted by historical reliability.\n\n## Decision Logic\n1. Collect signals from all sources:\n   - Order book imbalance -> UP/DOWN signal\n   - Candle momentum (last 3) -> UP/DOWN signal\n   - Funding rate sentiment -> UP/DOWN signal\n   - Long/short ratio -> UP/DOWN signal\n   - Taker volume -> UP/DOWN signal\n   - Polymarket consensus -> contrarian signal if extreme\n\n2. Initial weights (equal: 1.0 each)\n3. For each signal, add weight to UP or DOWN bucket\n4. Predict whichever bucket has higher total weight\n5. Confidence = winning_weight / total_weight\n\n## Self-Modification Notes\nAfter each evaluation window, analyze which signals were most predictive and adjust weights.\n\n## Confidence\n- Strong agreement (>4 signals aligned): 75%\n- Moderate agreement (3 signals): 60%\n- Split signals: 50%\n""",
    },
]


class AgentSpawner:
    def __init__(self, agents_dir: str):
        self.agents_dir = agents_dir
        os.makedirs(agents_dir, exist_ok=True)

    def _next_id(self) -> int:
        existing = []
        if os.path.isdir(self.agents_dir):
            for name in os.listdir(self.agents_dir):
                if name.startswith("agent-"):
                    try:
                        id_part = int(name.split("-")[1])
                        existing.append(id_part)
                    except (ValueError, IndexError):
                        pass
        return max(existing, default=0) + 1

    def spawn_from_seed(self, seed: dict) -> str:
        agent_id = self._next_id()
        agent_name = f"agent-{agent_id:03d}-{seed['name']}"
        agent_dir = os.path.join(self.agents_dir, agent_name)
        os.makedirs(agent_dir, exist_ok=True)
        os.makedirs(os.path.join(agent_dir, "scripts"), exist_ok=True)
        with open(os.path.join(agent_dir, "strategy.md"), "w") as f:
            f.write(seed["strategy"])
        with open(os.path.join(agent_dir, "notes.md"), "w") as f:
            f.write(f"# Notes for {agent_name}\n\nSpawned from seed: {seed['name']}\n")
        with open(os.path.join(agent_dir, "results.tsv"), "w") as f:
            f.write("iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n")
        logger.info(f"Spawned agent {agent_name} from seed {seed['name']}")
        return agent_name

    def clone_agent(self, source_name: str, mutation_note: str) -> str:
        source_dir = os.path.join(self.agents_dir, source_name)
        agent_id = self._next_id()
        clone_name = f"agent-{agent_id:03d}-clone-{source_name.split('-', 2)[-1]}"
        clone_dir = os.path.join(self.agents_dir, clone_name)
        shutil.copytree(source_dir, clone_dir)
        pred_path = os.path.join(clone_dir, "predictions.jsonl")
        if os.path.exists(pred_path):
            os.unlink(pred_path)
        results_path = os.path.join(clone_dir, "results.tsv")
        with open(results_path, "w") as f:
            f.write("iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n")
        notes_path = os.path.join(clone_dir, "notes.md")
        with open(notes_path, "a") as f:
            f.write(f"\n## Coordinator Mutation\nCloned from {source_name}.\nMutation instruction: {mutation_note}\n")
        logger.info(f"Cloned {source_name} -> {clone_name} with mutation: {mutation_note}")
        return clone_name

    def retire_agent(self, agent_name: str, graveyard_dir: str):
        agent_dir = os.path.join(self.agents_dir, agent_name)
        dest = os.path.join(graveyard_dir, agent_name)
        os.makedirs(graveyard_dir, exist_ok=True)
        shutil.move(agent_dir, dest)
        logger.info(f"Retired agent {agent_name} to graveyard")
