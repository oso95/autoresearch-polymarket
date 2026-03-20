import json


def _candles(snapshot: dict, limit: int | None = None) -> list[dict]:
    candles = snapshot.get("binance_candles_5m", {}).get("candles", []) or []
    return candles[-limit:] if limit else candles


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _last_polling(snapshot: dict, key: str, field: str):
    data = snapshot.get("polling", {}).get(key, {}).get("data", []) or []
    if not data:
        return None
    return data[-1].get(field)


def _polling_series(snapshot: dict, key: str) -> list[dict]:
    return snapshot.get("polling", {}).get(key, {}).get("data", []) or []


def _current_price(snapshot: dict) -> float:
    candles = _candles(snapshot, 1)
    if candles:
        return _safe_float(candles[-1].get("close"))
    return _safe_float(snapshot.get("chainlink_btc_price", {}).get("price"))


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    return sum(sample) / len(sample)


def _sma_slope(snapshot: dict, window: int = 20, compare: int = 5) -> float:
    closes = [_safe_float(c.get("close")) for c in _candles(snapshot, max(window, compare * 4))]
    if len(closes) < window:
        return 0.0
    sample = closes[-window:]
    if len(sample) < compare * 2:
        return 0.0
    first = sum(sample[:compare]) / compare
    last = sum(sample[-compare:]) / compare
    return (last - first) / first if first else 0.0


def _true_range(candle: dict, prev_close: float | None) -> float:
    high = _safe_float(candle.get("high"))
    low = _safe_float(candle.get("low"))
    if prev_close is None:
        return max(0.0, high - low)
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr(snapshot: dict, window: int) -> float:
    candles = _candles(snapshot, window + 1)
    if len(candles) < 2:
        return 0.0
    ranges = []
    prev_close = None
    for candle in candles[-window:]:
        ranges.append(_true_range(candle, prev_close))
        prev_close = _safe_float(candle.get("close"))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _book_imbalance(book: dict) -> float:
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []
    bid_size = sum(_safe_float(level.get("size", level.get("qty", 0))) for level in bids[:5])
    ask_size = sum(_safe_float(level.get("size", level.get("qty", 0))) for level in asks[:5])
    total = bid_size + ask_size
    if total <= 0:
        return 0.5
    return bid_size / total


def predict_orderbook_specialist(snapshot: dict) -> dict:
    poly_score = _book_imbalance(snapshot.get("polymarket_orderbook", {}))
    binance_score = _book_imbalance(snapshot.get("binance_orderbook", {}))
    score = poly_score * 0.7 + binance_score * 0.3
    prediction = "Up" if score >= 0.5 else "Down"
    confidence = min(0.9, 0.5 + abs(score - 0.5) * 1.6)
    return {
        "prediction": prediction,
        "confidence": confidence,
        "reasoning": f"orderbook imbalance poly={poly_score:.2f} binance={binance_score:.2f}",
    }


def predict_momentum_trader(snapshot: dict) -> dict:
    recent = _candles(snapshot, 3)
    if len(recent) < 3:
        return {"prediction": "Up", "confidence": 0.5, "reasoning": "not enough candles"}
    closes = [_safe_float(c["close"]) for c in recent]
    opens = [_safe_float(c["open"]) for c in recent]
    volumes = [_safe_float(c.get("volume")) for c in recent]
    greens = sum(1 for o, c in zip(opens, closes) if c >= o)
    momentum = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0.0
    volume_trend = volumes[-1] >= volumes[0]
    if greens == 3 and volume_trend:
        prediction, confidence = "Up", 0.75
    elif greens == 0 and volume_trend:
        prediction, confidence = "Down", 0.75
    else:
        prediction = "Up" if momentum >= 0 else "Down"
        confidence = 0.55 + min(abs(momentum) * 20, 0.15)
    return {
        "prediction": prediction,
        "confidence": min(confidence, 0.85),
        "reasoning": f"3-candle momentum={momentum:.4f} greens={greens}/3 volume_trend={volume_trend}",
    }


def predict_derivatives_analyst(snapshot: dict) -> dict:
    funding = _last_polling(snapshot, "funding_rate", "funding_rate")
    taker = _last_polling(snapshot, "taker_volume", "buy_sell_ratio")
    long_short = _last_polling(snapshot, "long_short_ratio", "long_short_ratio")
    signals = []
    score = 0.5
    if isinstance(funding, (int, float)):
        if funding > 0.00005:
            score -= 0.18
            signals.append("positive funding -> fade longs")
        elif funding < -0.00005:
            score += 0.18
            signals.append("negative funding -> fade shorts")
    if isinstance(taker, (int, float)):
        if taker > 1.2:
            score += 0.12
            signals.append("aggressive buying")
        elif taker < 0.8:
            score -= 0.12
            signals.append("aggressive selling")
    if isinstance(long_short, (int, float)):
        if long_short > 1.2:
            score -= 0.08
            signals.append("crowded longs")
        elif long_short < 0.8:
            score += 0.08
            signals.append("crowded shorts")
    prediction = "Up" if score >= 0.5 else "Down"
    confidence = 0.55 + min(abs(score - 0.5) * 1.5, 0.25)
    return {
        "prediction": prediction,
        "confidence": min(confidence, 0.8),
        "reasoning": ", ".join(signals) if signals else "no derivatives edge, neutral fallback",
    }


def predict_contrarian(snapshot: dict) -> dict:
    book = snapshot.get("polymarket_orderbook", {})
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []
    best_bid = _safe_float(bids[0].get("price", 0)) if bids else 0.0
    best_ask = _safe_float(asks[0].get("price", 1)) if asks else 1.0
    mid = (best_bid + best_ask) / 2 if (bids or asks) else 0.5
    if mid > 0.68:
        return {"prediction": "Down", "confidence": 0.64, "reasoning": f"consensus too bullish at {mid:.2f}"}
    if mid < 0.32:
        return {"prediction": "Up", "confidence": 0.64, "reasoning": f"consensus too bearish at {mid:.2f}"}
    candles = _candles(snapshot, 2)
    if len(candles) >= 2:
        prediction = "Up" if _safe_float(candles[-1]["close"]) >= _safe_float(candles[-2]["close"]) else "Down"
    else:
        prediction = "Up"
    return {"prediction": prediction, "confidence": 0.5, "reasoning": f"midpoint neutral at {mid:.2f}"}


def predict_mean_reversion(snapshot: dict) -> dict:
    candles = _candles(snapshot, 20)
    closes = [_safe_float(c["close"]) for c in candles]
    if len(closes) < 20:
        return {"prediction": "Up", "confidence": 0.5, "reasoning": "not enough candles"}
    current = closes[-1]
    sma20 = sum(closes) / len(closes)
    deviation = (current - sma20) / sma20 if sma20 else 0.0
    if deviation > 0.0015:
        prediction = "Down"
    elif deviation < -0.0015:
        prediction = "Up"
    else:
        prediction = "Up" if closes[-1] >= closes[-2] else "Down"
    confidence = 0.5 + min(abs(deviation) * 100, 0.2)
    return {
        "prediction": prediction,
        "confidence": min(confidence, 0.7),
        "reasoning": f"sma20 deviation={deviation:.4%}",
    }


def predict_volume_spike_detector(snapshot: dict) -> dict:
    candles = _candles(snapshot, 20)
    if len(candles) < 5:
        return {"prediction": "Up", "confidence": 0.5, "reasoning": "not enough candles"}
    volumes = [_safe_float(c.get("volume")) for c in candles]
    avg = sum(volumes[:-1]) / max(1, len(volumes) - 1)
    last = candles[-1]
    last_vol = volumes[-1]
    if avg > 0 and last_vol > avg * 1.8:
        prediction = "Up" if _safe_float(last["close"]) >= _safe_float(last["open"]) else "Down"
        confidence = 0.72
        reason = f"volume spike {last_vol/avg:.2f}x with candle direction"
    else:
        prev = candles[-2]
        prediction = "Up" if _safe_float(last["close"]) >= _safe_float(prev["close"]) else "Down"
        confidence = 0.52
        reason = "no volume spike, follow recent price direction"
    return {"prediction": prediction, "confidence": confidence, "reasoning": reason}


def predict_spread_analyzer(snapshot: dict) -> dict:
    book = snapshot.get("polymarket_orderbook", {})
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []
    if not bids or not asks:
        return predict_orderbook_specialist(snapshot)
    best_bid = _safe_float(bids[0].get("price"))
    best_ask = _safe_float(asks[0].get("price"))
    spread = max(0.0, best_ask - best_bid)
    bid_depth = sum(_safe_float(level.get("size", level.get("qty", 0))) for level in bids[:5])
    ask_depth = sum(_safe_float(level.get("size", level.get("qty", 0))) for level in asks[:5])
    if spread < 0.05 and bid_depth > ask_depth * 2:
        return {"prediction": "Up", "confidence": 0.7, "reasoning": f"narrow spread {spread:.3f}, bid depth dominates"}
    if spread < 0.05 and ask_depth > bid_depth * 2:
        return {"prediction": "Down", "confidence": 0.7, "reasoning": f"narrow spread {spread:.3f}, ask depth dominates"}
    candles = _candles(snapshot, 2)
    prediction = "Up"
    if len(candles) >= 2:
        prediction = "Up" if _safe_float(candles[-1]["close"]) >= _safe_float(candles[-2]["close"]) else "Down"
    confidence = 0.55 if spread > 0.10 else 0.52
    return {"prediction": prediction, "confidence": confidence, "reasoning": f"spread={spread:.3f}"}


def predict_regime_detector(snapshot: dict) -> dict:
    recent_atr = _atr(snapshot, 20)
    long_atr = _atr(snapshot, 50)
    candles = _candles(snapshot, 3)
    if len(candles) < 2 or long_atr <= 0:
        return predict_mean_reversion(snapshot)
    last = candles[-1]
    prev = candles[-2]
    if recent_atr > long_atr * 1.3:
        prediction = "Up" if _safe_float(last["close"]) >= _safe_float(prev["close"]) else "Down"
        return {"prediction": prediction, "confidence": 0.64, "reasoning": "volatile regime, follow momentum"}
    if recent_atr < long_atr * 0.8:
        prediction = "Down" if _safe_float(last["close"]) >= _safe_float(last["open"]) else "Up"
        return {"prediction": prediction, "confidence": 0.58, "reasoning": "quiet regime, fade last candle"}
    slope = _sma_slope(snapshot)
    prediction = "Up" if slope >= 0 else "Down"
    return {"prediction": prediction, "confidence": 0.55, "reasoning": f"neutral regime, slope={slope:.4%}"}


def predict_funding_rate_velocity(snapshot: dict) -> dict:
    funding_data = _polling_series(snapshot, "funding_rate")
    if len(funding_data) >= 2:
        recent = funding_data[-2:]
        latest = _safe_float(recent[-1].get("funding_rate"))
        prev = _safe_float(recent[-2].get("funding_rate"))
        velocity = latest - prev
        if abs(latest) > 0.00005 and abs(velocity) > 0.00002:
            prediction = "Down" if latest > 0 else "Up"
            confidence = 0.8 if abs(velocity) > 0.00004 else 0.75
            return {
                "prediction": prediction,
                "confidence": confidence,
                "reasoning": f"funding velocity spike latest={latest:.6f} prev={prev:.6f}",
            }
    # Regime-aware fallback adapted from the strategy notes.
    closes = [_safe_float(c["close"]) for c in _candles(snapshot, 20)]
    if len(closes) < 20:
        return predict_derivatives_analyst(snapshot)
    current = closes[-1]
    sma20 = sum(closes) / len(closes)
    slope = _sma_slope(snapshot)
    deviation = (current - sma20) / sma20 if sma20 else 0.0
    taker = _last_polling(snapshot, "taker_volume", "buy_sell_ratio")
    if slope < -0.005:
        if deviation <= -0.02:
            prediction, confidence = "Up", 0.58
        else:
            prediction, confidence = "Down", 0.58
    elif slope < -0.002:
        if deviation <= -0.008:
            prediction, confidence = "Up", 0.60
        elif deviation >= 0.008:
            prediction, confidence = "Down", 0.60
        else:
            prediction, confidence = "Down", 0.52
    elif slope > 0.002:
        if deviation >= 0.008:
            prediction, confidence = "Down", 0.60
        elif deviation <= -0.008:
            prediction, confidence = "Up", 0.60
        else:
            prediction, confidence = "Up", 0.52
    elif abs(slope) >= 0.0005:
        if abs(deviation) >= 0.004:
            prediction = "Down" if deviation > 0 else "Up"
            confidence = 0.58
        else:
            prediction = "Up" if slope > 0 else "Down"
            confidence = 0.52
    else:
        if abs(deviation) >= 0.004:
            prediction = "Down" if deviation > 0 else "Up"
            confidence = 0.60
        else:
            prediction, confidence = "Up", 0.50
    if prediction == "Up" and isinstance(taker, (int, float)) and deviation < 0 and taker > 1.10:
        prediction, confidence = "Down", 0.52
    elif prediction == "Down" and isinstance(taker, (int, float)) and deviation > 0 and taker < 0.90:
        prediction, confidence = "Up", 0.52
    elif isinstance(taker, (int, float)):
        confidence = min(confidence + 0.03, 0.63) if (
            (prediction == "Up" and taker < 0.75 and deviation < 0) or
            (prediction == "Down" and taker > 1.30 and deviation > 0)
        ) else confidence
    return {
        "prediction": prediction,
        "confidence": confidence,
        "reasoning": f"funding fallback slope={slope:.4%} deviation={deviation:.4%}",
    }


def _predict_member(member: str, snapshot: dict) -> dict | None:
    if "orderbook-specialist" in member:
        return predict_orderbook_specialist(snapshot)
    if "momentum-trader" in member:
        return predict_momentum_trader(snapshot)
    if "derivatives-analyst" in member:
        return predict_derivatives_analyst(snapshot)
    if "contrarian" in member:
        return predict_contrarian(snapshot)
    if "regime-detector" in member:
        return predict_regime_detector(snapshot)
    if "mean-reversion" in member:
        return predict_mean_reversion(snapshot)
    if "volume-spike-detector" in member:
        return predict_volume_spike_detector(snapshot)
    if "spread-analyzer" in member:
        return predict_spread_analyzer(snapshot)
    return None


def _weighted_vote(member_results: list[tuple[str, float, dict]]) -> dict:
    up_weight = 0.0
    down_weight = 0.0
    reasons = []
    for member, weight, result in member_results:
        conf = float(result.get("confidence", 0.5))
        weighted = weight * conf
        if result["prediction"] == "Up":
            up_weight += weighted
        else:
            down_weight += weighted
        reasons.append(f"{member}={result['prediction']}@{weighted:.2f}")
    total = up_weight + down_weight
    if total <= 0:
        return {"prediction": "Up", "confidence": 0.5, "reasoning": "no member votes"}
    prediction = "Up" if up_weight >= down_weight else "Down"
    confidence = max(up_weight, down_weight) / total
    return {
        "prediction": prediction,
        "confidence": max(0.51, min(confidence, 0.85)),
        "reasoning": "; ".join(reasons[:8]),
    }


def predict_ensemble_top2_clones(snapshot: dict) -> dict:
    members = [
        ("agent-014-clone-clone-derivatives-analyst", 0.60),
        ("agent-012-clone-derivatives-analyst", 0.58),
    ]
    results = [(member, weight, _predict_member(member, snapshot)) for member, weight in members]
    return _weighted_vote([(m, w, r) for m, w, r in results if r is not None])


def predict_ensemble_top3_derivatives(snapshot: dict) -> dict:
    members = [
        ("agent-014-clone-clone-derivatives-analyst", 0.60),
        ("agent-012-clone-derivatives-analyst", 0.58),
        ("agent-003-derivatives-analyst", 0.50),
    ]
    vote = _weighted_vote([(m, w, _predict_member(m, snapshot)) for m, w in members])
    slope = _sma_slope(snapshot)
    if slope < -0.0005 and vote["prediction"] == "Up" and vote["confidence"] < 0.65:
        return {"prediction": "Down", "confidence": 0.52, "reasoning": f"{vote['reasoning']}; downtrend override"}
    if slope > 0.0005 and vote["prediction"] == "Down" and vote["confidence"] < 0.65:
        return {"prediction": "Up", "confidence": 0.52, "reasoning": f"{vote['reasoning']}; uptrend override"}
    return vote


def predict_ensemble_top5_best(snapshot: dict) -> dict:
    members = [
        ("agent-014-clone-clone-derivatives-analyst", 0.60),
        ("agent-012-clone-derivatives-analyst", 0.58),
        ("agent-004-contrarian", 0.50),
        ("agent-010-regime-detector", 0.50),
        ("agent-003-derivatives-analyst", 0.48),
    ]
    return _weighted_vote([(m, w, _predict_member(m, snapshot)) for m, w in members])


def predict_ensemble_all_agents(snapshot: dict) -> dict:
    members = [
        ("agent-012-clone-derivatives-analyst", 0.58),
        ("agent-004-contrarian", 0.50),
        ("agent-003-derivatives-analyst", 0.48),
        ("agent-010-regime-detector", 0.50),
        ("agent-002-momentum-trader", 0.46),
        ("agent-007-mean-reversion", 0.50),
        ("agent-006-volume-spike-detector", 0.47),
        ("agent-013-clone-clone-derivatives-analyst", 0.48),
        ("agent-011-clone-contrarian", 0.49),
        ("agent-008-spread-analyzer", 0.40),
        ("agent-001-orderbook-specialist", 0.46),
    ]
    return _weighted_vote([(m, w, _predict_member(m, snapshot)) for m, w in members])


def run_predictor(kind: str, snapshot_path: str):
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    if kind == "agent-032":
        result = predict_ensemble_top2_clones(snapshot)
    elif kind == "agent-034":
        result = predict_ensemble_top3_derivatives(snapshot)
    elif kind == "agent-037":
        result = predict_ensemble_top5_best(snapshot)
    elif kind == "agent-039":
        result = predict_ensemble_all_agents(snapshot)
    elif kind == "agent-057":
        result = predict_funding_rate_velocity(snapshot)
    else:
        raise SystemExit(f"unknown predictor kind: {kind}")
    print(json.dumps(result))
