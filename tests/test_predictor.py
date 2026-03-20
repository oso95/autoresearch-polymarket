import asyncio
import json
import pytest
from src.runner.predictor import Predictor, parse_prediction_response

def test_parse_prediction_response_up():
    response = '{"prediction": "Up", "confidence": 0.72, "reasoning": "OB imbalance bullish"}'
    result = parse_prediction_response(response)
    assert result["prediction"] == "Up"
    assert result["confidence"] == 0.72
    assert result["reasoning"] == "OB imbalance bullish"

def test_parse_prediction_response_down():
    response = '{"prediction": "Down", "confidence": 0.65, "reasoning": "Momentum bearish"}'
    result = parse_prediction_response(response)
    assert result["prediction"] == "Down"

def test_parse_prediction_response_invalid():
    result = parse_prediction_response("I have no idea what will happen")
    assert result is None

def test_parse_prediction_response_missing_fields():
    response = '{"prediction": "Up"}'
    result = parse_prediction_response(response)
    assert result["prediction"] == "Up"
    assert result["confidence"] == 0.5
    assert result["reasoning"] == ""

def test_build_prediction_prompt():
    predictor = Predictor(timeout_seconds=90)
    prompt = predictor.build_prompt(
        strategy="# My Strategy\nBuy when up",
        snapshot={"chainlink_btc_price": {"price": 65000}},
        scripts={"calc.py": "print('hello')"},
        recent_results="win,win,loss",
        notes="Try order book imbalance"
    )
    assert "65000" in prompt
    assert "My Strategy" in prompt
    assert "calc.py" in prompt
    assert "JSON" in prompt

def test_get_prediction_prefers_script_execution(tmp_path):
    agent_dir = tmp_path / "agent"
    scripts_dir = agent_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script_path = scripts_dir / "predict.py"
    script_path.write_text(
        "import json, sys\n"
        "with open(sys.argv[1]) as f:\n"
        "    snapshot = json.load(f)\n"
        "price = snapshot['chainlink_btc_price']['price']\n"
        "print(json.dumps({'prediction': 'Up' if price > 60000 else 'Down', 'confidence': 0.83, 'reasoning': 'script'}))\n"
    )
    predictor = Predictor(timeout_seconds=5)
    result = asyncio.run(
        predictor.get_prediction(
            agent_dir=str(agent_dir),
            strategy="# strategy",
            snapshot={"chainlink_btc_price": {"price": 65000}},
            scripts={"predict.py": script_path.read_text()},
            recent_results="",
            notes="",
        )
    )
    assert result["prediction"] == "Up"
    assert result["confidence"] == pytest.approx(0.83)
    assert "Script-first" in result["reasoning"]

def test_get_batch_predictions_uses_script_signals(tmp_path):
    agent_dir = tmp_path / "agent"
    scripts_dir = agent_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script_path = scripts_dir / "signal.py"
    script_path.write_text(
        "import json, sys\n"
        "with open(sys.argv[1]) as f:\n"
        "    snapshot = json.load(f)\n"
        "price = snapshot['chainlink_btc_price']['price']\n"
        "score = 0.8 if price > 60000 else 0.2\n"
        "print(json.dumps({'btc_signal': score, 'confidence': 0.9}))\n"
    )
    predictor = Predictor(timeout_seconds=5)
    results = asyncio.run(
        predictor.get_batch_predictions(
            agent_dir=str(agent_dir),
            strategy="# strategy",
            snapshots=[
                {"chainlink_btc_price": {"price": 65000}},
                {"chainlink_btc_price": {"price": 55000}},
            ],
            scripts={"signal.py": script_path.read_text()},
            notes="",
        )
    )
    assert results[0]["prediction"] == "Up"
    assert results[1]["prediction"] == "Down"
    assert "Script-first" in results[0]["reasoning"]
