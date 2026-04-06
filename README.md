# Autoresearch Polymarket

Autonomous strategy discovery for Polymarket BTC 5-minute markets.

This repo runs a local tournament of agents that:

- read live and historical market data
- make `Up` / `Down` predictions
- evolve their strategies over time
- backtest, clone, mirror, and ensemble top performers

> **VPN Required:** Polymarket and Binance restrict access from certain countries and regions. If you are in a restricted area, you will need a VPN to use this system. Check the [Polymarket Terms of Service](https://polymarket.com/tos) and [Binance regional availability](https://www.binance.com/en/support) for details.

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

## Benchmark Results

Live trading results from March 18–21, 2026 on Polymarket BTC 5-minute Up/Down markets using Codex (GPT-5.4). Raw transaction history is in [`Polymarket-History-2026-04-06.csv`](Polymarket-History-2026-04-06.csv).

| Metric | Value |
|--------|-------|
| Rounds traded | 16 |
| Win rate | 43.8% (7W / 9L) |
| Total deployed | $183.74 |
| Total returned | $257.26 |
| Net P&L | **+$73.52** |
| ROI | **+40.0%** |

The system's edge comes from entry pricing and position sizing rather than raw directional accuracy — wins are significantly larger than losses.

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

## License

[MIT](LICENSE)
