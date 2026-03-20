# tests/test_integration.py
import json
import os
import pytest
from src.config import Config
from src.data_layer.round_manager import RoundManager
from src.runner.agent_runner import AgentRunner, build_agent_context, score_prediction
from src.runner.paper_execution import build_execution_quote
from src.coordinator.spawner import AgentSpawner, SEED_STRATEGIES
from src.coordinator.tournament import Tournament
from src.coordinator.leaderboard import build_leaderboard
from src.io_utils import atomic_write_json, atomic_append_jsonl

@pytest.fixture
def project(tmp_path):
    data_dir = str(tmp_path / "data")
    agents_dir = str(tmp_path / "agents")
    for d in ["live", "polling", "history", "rounds", "coordinator"]:
        os.makedirs(os.path.join(data_dir, d))
    os.makedirs(agents_dir)
    atomic_write_json(os.path.join(data_dir, "live", "chainlink_btc_price.json"), {"price": 65000.0, "timestamp": 1710000000000})
    atomic_write_json(os.path.join(data_dir, "live", "binance_candles_5m.json"), {"candles": [{"open": 64950, "close": 65000, "high": 65100, "low": 64900, "volume": 100, "open_time": 1709999700000, "close_time": 1709999999999, "trades": 500, "closed": True}]})
    atomic_write_json(os.path.join(data_dir, "live", "binance_orderbook.json"), {"bids": [{"price": 64999, "qty": 2}], "asks": [{"price": 65001, "qty": 1.5}]})
    atomic_write_json(os.path.join(data_dir, "live", "polymarket_orderbook.json"), {"bids": [{"price": 0.55, "size": 500}], "asks": [{"price": 0.60, "size": 300}]})
    atomic_write_json(
        os.path.join(data_dir, "live", "polymarket_orderbooks.json"),
        {"books": {
            "Up": {"bids": [{"price": 0.55, "size": 500}], "asks": [{"price": 0.60, "size": 300}], "last_trade_price": 0.58},
            "Down": {"bids": [{"price": 0.40, "size": 500}], "asks": [{"price": 0.45, "size": 300}], "last_trade_price": 0.42},
        }},
    )
    atomic_write_json(
        os.path.join(data_dir, "live", "polymarket_market.json"),
        {"market_id": "test-market", "slug": "btc-updown-5m-1710000000", "token_map": {"Up": "token-up", "Down": "token-down"}},
    )
    for f in ["open_interest", "taker_volume", "long_short_ratio", "top_trader_ratio", "funding_rate"]:
        atomic_write_json(os.path.join(data_dir, "polling", f"{f}.json"), {"data": [], "stale": False})
    return data_dir, agents_dir

def test_full_round_lifecycle(project):
    data_dir, agents_dir = project
    config = Config(min_agents=2, max_agents=5)

    spawner = AgentSpawner(agents_dir)
    agent1 = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    agent2 = spawner.spawn_from_seed(SEED_STRATEGIES[1])

    rm = RoundManager(data_dir)
    rm.freeze_snapshot(1710000000)
    snapshot_path = os.path.join(data_dir, "rounds", "1710000000", "snapshot.json")
    assert os.path.exists(snapshot_path)
    with open(snapshot_path) as f:
        snapshot = json.load(f)

    agent1_dir = os.path.join(agents_dir, agent1)
    ctx = build_agent_context(agent1_dir, snapshot)
    assert "Order Book Specialist" in ctx
    assert "65000" in ctx

    runner = AgentRunner(agents_dir, data_dir)
    runner.record_prediction(agent1, 1710000000, "Up", 0.7, "OB imbalance bullish", "v1", execution_quote=build_execution_quote(snapshot, "Up"))
    runner.record_prediction(agent1, 1710000000, "Down", 0.8, "OB imbalance flipped", "v2", execution_quote=build_execution_quote(snapshot, "Down"))
    runner.record_prediction(agent2, 1710000000, "Down", 0.6, "Momentum bearish", "v1", execution_quote=build_execution_quote(snapshot, "Down"))

    shared_prediction_path = os.path.join(
        data_dir, "rounds", "1710000000", "predictions", f"{agent1}.json"
    )
    shared_updates_path = os.path.join(
        data_dir, "rounds", "1710000000", "prediction-updates", f"{agent1}.jsonl"
    )
    shared_ledger_path = os.path.join(data_dir, "live", "shared-ledger.json")
    status_path = os.path.join(agents_dir, agent1, "status.json")
    execution_path = os.path.join(agents_dir, agent1, "executions.jsonl")
    assert os.path.exists(shared_prediction_path)
    assert os.path.exists(shared_updates_path)
    assert os.path.exists(shared_ledger_path)
    assert os.path.exists(status_path)

    rm.record_resolution(1710000000, "Up", 65000.0, 65050.0)

    runner.score_round(agent1, 1710000000, "Up")
    runner.score_round(agent2, 1710000000, "Up")

    wr1, total1 = runner.get_agent_win_rate(agent1)
    wr2, total2 = runner.get_agent_win_rate(agent2)
    assert wr1 == 0.0
    assert wr2 == 0.0
    assert total1 == 1
    assert total2 == 1

    with open(shared_prediction_path) as f:
        shared_pred = json.load(f)
    assert shared_pred["correct"] is False
    assert shared_pred["revision"] == 2
    assert shared_pred["entry_price"] == 0.45

    assert os.path.exists(execution_path)

    with open(status_path) as f:
        status = json.load(f)
    assert status["total_rounds"] == 1
    assert status["total_correct"] == 0

def test_tournament_cycle_after_rounds(project):
    data_dir, agents_dir = project
    config = Config(min_agents=2, max_agents=5, kill_min_rounds=5, initial_screening_rounds=3)
    spawner = AgentSpawner(agents_dir)
    runner = AgentRunner(agents_dir, data_dir)
    tournament = Tournament(config, spawner, runner, data_dir)

    a1 = spawner.spawn_from_seed(SEED_STRATEGIES[0])
    a2 = spawner.spawn_from_seed(SEED_STRATEGIES[1])
    a3 = spawner.spawn_from_seed(SEED_STRATEGIES[2])

    win_patterns = {a1: [True]*8 + [False]*2, a2: [True]*5 + [False]*5, a3: [True]*2 + [False]*8}
    for i in range(10):
        for name in [a1, a2, a3]:
            correct = win_patterns[name][i]
            pred = "Up" if correct else "Down"
            outcome = "Up"
            atomic_append_jsonl(
                os.path.join(agents_dir, name, "predictions.jsonl"),
                {"round": i, "agent": name, "prediction": pred, "outcome": outcome, "correct": correct, "confidence": 0.5, "reasoning": "test", "strategy_version": "v1"}
            )

    result = tournament.run_cycle()

    lb_path = os.path.join(data_dir, "coordinator", "leaderboard.json")
    assert os.path.exists(lb_path)
    with open(lb_path) as f:
        lb = json.load(f)
    assert len(lb["entries"]) >= 2
