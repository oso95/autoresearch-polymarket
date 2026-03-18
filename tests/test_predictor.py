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
