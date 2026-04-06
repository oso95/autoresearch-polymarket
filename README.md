# Autoresearch Polymarket

[English](README.md) | [繁體中文](README.zh-TW.md)

A proof-of-concept applying the [autoresearch](https://github.com/uditgoenka/autoresearch) architecture — a Claude-based fork of Karpathy's [autoresearch](https://github.com/karpathy/autoresearch) — to a real-world use case: Polymarket BTC 5-minute prediction markets.

This project demonstrates how autonomous agents can collaborate through a shared knowledge system, evolve strategies via tournament selection, and improve over time without human intervention. Polymarket serves as the live feedback loop for validating the architecture.

> **This is not a trading tool.** It is a research prototype proving that the autoresearch multi-agent architecture works in practice. The benchmark results below exist solely to demonstrate a functioning system, not to encourage trading.

> **Token Usage Warning:** This system invokes LLM calls (Codex/Claude) continuously — once per agent per 5-minute round, plus evolution, backtesting, and coordination calls. Expect significant API token consumption. Monitor your usage and billing closely.

> **VPN Required:** Polymarket and Binance restrict access from certain countries and regions. If you are in a restricted area, you will need a VPN to use this system. Check the [Polymarket Terms of Service](https://polymarket.com/tos) and [Binance regional availability](https://www.binance.com/en/support) for details.

## How It Works

The system runs a continuous loop: agents make predictions, get scored against real outcomes, evolve their strategies, and share what they learn — all autonomously.

```
┌─────────────────────────────────────────────────────────┐
│                   ORCHESTRATION LOOP                    │
│                                                         │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │  DETECT  │──▶│ PREDICT  │──▶│  SCORE   │            │
│  │  ROUND   │   │ (agents) │   │ (outcome)│            │
│  └──────────┘   └──────────┘   └────┬─────┘            │
│                                     │                   │
│       ┌─────────────────────────────┘                   │
│       ▼                                                 │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │  EVOLVE  │──▶│TOURNAMENT│──▶│  REPEAT  │            │
│  │(策略演化) │   │(淘汰/複製)│   │          │            │
│  └──────────┘   └──────────┘   └──────────┘            │
└─────────────────────────────────────────────────────────┘
```

### Shared Knowledge Forum

Agents don't just compete — they collaborate through a shared knowledge forum where they can:

- **Post discoveries** — after evolving, agents publish insights (e.g. "bearish bias in Asian session hours")
- **Upvote / downvote posts** — agents vote on each other's ideas based on their own experience
- **Comment on posts** — agents discuss and build on each other's findings
- Posts are ranked by score and surfaced to all agents during evolution, so high-quality ideas propagate across the population

### Tournament Selection

The population is under constant evolutionary pressure:

- **Kill** — agents below 30% win rate after screening are removed; sustained underperformers (below 45%) are culled
- **Clone** — top 2-3 agents are cloned with diverse mutations (e.g. "try more aggressive thresholds", "add time-of-day weighting")
- **Mirror** — agents with extremely low win rates (<40%) get inverted — if an agent is consistently wrong, its mirror should be consistently right
- **Ensemble** — top agents are combined into voting ensembles
- **Seed** — when population drops, new agents are spawned from 16 seed strategies (including unconventional ones like Yi Jing oracle, Fibonacci spiral, and crowd psychology)

### Strategy Evolution

Every K rounds, agents enter an evolution cycle:

1. **Review** — read own results, shared knowledge forum, leaderboard, and decision quality profile
2. **Ideate** — identify what patterns wins/losses share, what top agents do differently
3. **Modify** — rewrite strategy and prediction scripts via LLM
4. **Test** — evaluate new strategy over next 5 rounds
5. **Keep or discard** — if win rate improves, keep the change; if it declines, auto-revert

Each agent maintains a memory chain of all evolution attempts (kept and discarded) so it learns from its own history.

### Fast-Fail Safety

If an evolved strategy causes 3+ consecutive losses, the system automatically reverts to the previous working version — allowing aggressive exploration with guardrails.

## Requirements

- Python `>=3.11`
- One of:
  - `codex` on your `PATH`
  - `claude` on your `PATH`

Install Python dependencies:

```bash
pip install -e '.[dev]'
```

## Quick Start

Initialize a project workspace:

```bash
python3 -m src.main init --dir ./my-run
```

Run the live system:

```bash
python3 autoresearch_local.py --dir ./my-run live
```

Run a backtest:

```bash
python3 backtest.py --dir ./my-run
```

Run fast evolution on historical rounds:

```bash
python3 fast_evolve.py --dir ./my-run --iterations 3
```

## Model Runtime

The default runtime is controlled by environment variables.

Default to Codex:

```bash
export AUTORESEARCH_MODEL_PROVIDER=codex
```

Default to Claude:

```bash
export AUTORESEARCH_MODEL_PROVIDER=claude
```

You can also override per call or per agent with model names:

- Codex examples:
  - `gpt-5.4`
  - `codex:gpt-5.4`
- Claude examples:
  - `sonnet`
  - `opus`
  - `claude:sonnet`

Per-agent overrides live in `agent_config.json`. Example:

```json
{
  "model": "claude:sonnet"
}
```

Useful environment variables are documented in [.env.example](.env.example).

The neutral runtime implementation lives in [`src/model_cli.py`](src/model_cli.py). [`src/codex_cli.py`](src/codex_cli.py) remains as a compatibility shim for older imports.

## Config

See [`config.example.json`](config.example.json) for a complete example config. Copy it to `config.json` and adjust as needed:

```bash
cp config.example.json config.json
```

## Proof of Working System

Live results from March 18–21, 2026 on Polymarket BTC 5-minute Up/Down markets using Codex (GPT-5.4). These results demonstrate that the architecture produces a functioning autonomous system, not that it is a reliable trading strategy. Raw transaction history is in [`Polymarket-History-2026-04-06.csv`](Polymarket-History-2026-04-06.csv).

| Metric | Value |
|--------|-------|
| Rounds traded | 16 |
| Win rate | 43.8% (7W / 9L) |
| Total deployed | $183.74 |
| Total returned | $257.26 |
| Net P&L | **+$73.52** |
| ROI | **+40.0%** |

## Testing

```bash
pytest
```

## Examples

See [`examples/README.md`](examples/README.md) for checked-in example projects showing real agent outputs and tournament state.

## Layout

| Path | Description |
|------|-------------|
| [`src/`](src) | Core runtime |
| [`tests/`](tests) | Test suite |
| [`examples/`](examples) | Compact example projects and findings |
| [`backtest.py`](backtest.py) | Historical evaluation |
| [`fast_evolve.py`](fast_evolve.py) | Accelerated evolution loop |
| [`strategy_factory.py`](strategy_factory.py) | Continuous optimization loop |
| [`paper_returns.py`](paper_returns.py) | Paper-trading P&L summary |
| [`autoresearch_local.py`](autoresearch_local.py) | Local entrypoint |

## Acknowledgements

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — original autoresearch concept
- [uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch) — Claude-based fork this project builds on

## License

[MIT](LICENSE)
