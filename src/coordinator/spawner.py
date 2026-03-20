# src/coordinator/spawner.py
import json
import os
import shutil
import logging

from src.io_utils import atomic_write_json
from src.memory_utils import init_memory

logger = logging.getLogger(__name__)

ORDERBOOK_SPECIALIST_SCRIPT = """#!/usr/bin/env python3
import json
import sys

def _book_imbalance(book):
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []
    bid_size = sum(float(level.get("size", level.get("qty", 0))) for level in bids[:5])
    ask_size = sum(float(level.get("size", level.get("qty", 0))) for level in asks[:5])
    total = bid_size + ask_size
    if total <= 0:
        return 0.5
    return bid_size / total

with open(sys.argv[1]) as f:
    snapshot = json.load(f)

poly = snapshot.get("polymarket_orderbook", {})
binance = snapshot.get("binance_orderbook", {})
poly_score = _book_imbalance(poly)
binance_score = _book_imbalance(binance)
score = poly_score * 0.7 + binance_score * 0.3
prediction = "Up" if score >= 0.5 else "Down"
confidence = min(0.9, 0.5 + abs(score - 0.5) * 1.6)
print(json.dumps({
    "prediction": prediction,
    "confidence": confidence,
    "reasoning": f"orderbook imbalance poly={poly_score:.2f} binance={binance_score:.2f}",
}))
"""

MOMENTUM_TRADER_SCRIPT = """#!/usr/bin/env python3
import json
import sys

with open(sys.argv[1]) as f:
    snapshot = json.load(f)

candles = snapshot.get("binance_candles_5m", {}).get("candles", [])
recent = candles[-3:]
if len(recent) < 3:
    print(json.dumps({"prediction": "Up", "confidence": 0.5, "reasoning": "not enough candles"}))
    raise SystemExit

closes = [float(c["close"]) for c in recent]
opens = [float(c["open"]) for c in recent]
volumes = [float(c.get("volume", 0)) for c in recent]
greens = sum(1 for o, c in zip(opens, closes) if c >= o)
momentum = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0.0
volume_trend = volumes[-1] >= volumes[0]

if greens == 3 and volume_trend:
    prediction = "Up"
    confidence = 0.75
elif greens == 0 and volume_trend:
    prediction = "Down"
    confidence = 0.75
else:
    prediction = "Up" if momentum >= 0 else "Down"
    confidence = 0.55 + min(abs(momentum) * 20, 0.15)

print(json.dumps({
    "prediction": prediction,
    "confidence": min(confidence, 0.85),
    "reasoning": f"3-candle momentum={momentum:.4f} greens={greens}/3 volume_trend={volume_trend}",
}))
"""

DERIVATIVES_ANALYST_SCRIPT = """#!/usr/bin/env python3
import json
import sys

def _last_value(snapshot, key, field):
    data = snapshot.get("polling", {}).get(key, {}).get("data", [])
    if not data:
        return None
    return data[-1].get(field)

with open(sys.argv[1]) as f:
    snapshot = json.load(f)

funding = _last_value(snapshot, "funding_rate", "funding_rate")
taker = _last_value(snapshot, "taker_volume", "buy_sell_ratio")
long_short = _last_value(snapshot, "long_short_ratio", "long_short_ratio")

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
print(json.dumps({
    "prediction": prediction,
    "confidence": min(confidence, 0.8),
    "reasoning": ", ".join(signals) if signals else "no derivatives edge, neutral fallback",
}))
"""

CONTRARIAN_SCRIPT = """#!/usr/bin/env python3
import json
import sys

with open(sys.argv[1]) as f:
    snapshot = json.load(f)

book = snapshot.get("polymarket_orderbook", {})
bids = book.get("bids", []) or []
asks = book.get("asks", []) or []
best_bid = float(bids[0].get("price", 0)) if bids else 0.0
best_ask = float(asks[0].get("price", 1)) if asks else 1.0
mid = (best_bid + best_ask) / 2 if bids or asks else 0.5

if mid > 0.68:
    prediction = "Down"
    confidence = 0.64
    reasoning = f"consensus too bullish at {mid:.2f}"
elif mid < 0.32:
    prediction = "Up"
    confidence = 0.64
    reasoning = f"consensus too bearish at {mid:.2f}"
else:
    candles = snapshot.get("binance_candles_5m", {}).get("candles", [])
    if len(candles) >= 2:
        prediction = "Up" if float(candles[-1]["close"]) >= float(candles[-2]["close"]) else "Down"
    else:
        prediction = "Up"
    confidence = 0.5
    reasoning = f"midpoint neutral at {mid:.2f}"

print(json.dumps({
    "prediction": prediction,
    "confidence": confidence,
    "reasoning": reasoning,
}))
"""

MULTI_SIGNAL_SYNTHESIZER_SCRIPT = """#!/usr/bin/env python3
import json
import sys

with open(sys.argv[1]) as f:
    snapshot = json.load(f)

votes = []

book = snapshot.get("polymarket_orderbook", {})
bids = book.get("bids", []) or []
asks = book.get("asks", []) or []
if bids or asks:
    bid_size = sum(float(level.get("size", level.get("qty", 0))) for level in bids[:5])
    ask_size = sum(float(level.get("size", level.get("qty", 0))) for level in asks[:5])
    total = bid_size + ask_size
    if total > 0:
        votes.append(bid_size / total)

candles = snapshot.get("binance_candles_5m", {}).get("candles", [])
if len(candles) >= 3:
    closes = [float(c["close"]) for c in candles[-3:]]
    votes.append(1.0 if closes[-1] > closes[0] else 0.0)

polling = snapshot.get("polling", {})
funding = polling.get("funding_rate", {}).get("data", [])
if funding:
    rate = funding[-1].get("funding_rate")
    if isinstance(rate, (int, float)):
        votes.append(0.25 if rate > 0.00005 else 0.75 if rate < -0.00005 else 0.5)

taker = polling.get("taker_volume", {}).get("data", [])
if taker:
    ratio = taker[-1].get("buy_sell_ratio")
    if isinstance(ratio, (int, float)):
        votes.append(0.75 if ratio > 1.15 else 0.25 if ratio < 0.85 else 0.5)

score = sum(votes) / len(votes) if votes else 0.5
prediction = "Up" if score >= 0.5 else "Down"
confidence = 0.5 + min(abs(score - 0.5), 0.3)
print(json.dumps({
    "prediction": prediction,
    "confidence": confidence,
    "reasoning": f"multi-signal average over {len(votes)} votes -> {score:.2f}",
}))
"""

SEED_STRATEGIES = [
    {
        "name": "orderbook-specialist",
        "strategy": """# Order Book Specialist\n\n## Focus\nAnalyze Polymarket order book to predict BTC 5-minute direction.\n\n## Data Sources\n- Primary: `polymarket_orderbook` (bids/asks depth and imbalance)\n- Secondary: `binance_orderbook` (cross-reference exchange book)\n\n## Decision Logic\n1. Calculate bid/ask imbalance ratio: total_bid_volume / total_ask_volume\n2. If imbalance > 1.5 (heavy buying pressure) -> predict UP\n3. If imbalance < 0.67 (heavy selling pressure) -> predict DOWN\n4. If neutral (0.67-1.5) -> look at which side has more levels near the spread\n5. Check if large orders appeared recently (> 2x average size)\n\n## Confidence\n- Strong imbalance (>2.0 or <0.5): 80%\n- Moderate imbalance: 60%\n- Neutral: 50%\n""",
        "scripts": {"predict.py": ORDERBOOK_SPECIALIST_SCRIPT},
    },
    {
        "name": "momentum-trader",
        "strategy": """# Momentum Trader\n\n## Focus\nUse Binance 5-minute candle patterns to predict next candle direction.\n\n## Data Sources\n- Primary: `binance_candles_5m` (last 100 candles OHLCV)\n- Secondary: `binance_orderbook`\n\n## Decision Logic\n1. Look at last 3 candles for trend direction\n2. Calculate simple momentum: (close[-1] - close[-3]) / close[-3]\n3. If last 3 candles all green AND volume increasing -> predict UP\n4. If last 3 candles all red AND volume increasing -> predict DOWN\n5. If mixed signals -> look at volume-weighted average price vs current\n6. Check for reversal patterns: long wicks, doji candles\n\n## Confidence\n- Strong 3-candle trend with volume: 75%\n- Weak trend or mixed: 55%\n- Reversal signal detected: 60% (reverse direction)\n""",
        "scripts": {"predict.py": MOMENTUM_TRADER_SCRIPT},
    },
    {
        "name": "derivatives-analyst",
        "strategy": """# Derivatives Analyst\n\n## Focus\nUse futures market data to predict spot BTC direction.\n\n## Data Sources\n- Primary: `polling/open_interest`, `polling/funding_rate`\n- Secondary: `polling/long_short_ratio`, `polling/taker_volume`, `polling/top_trader_ratio`\n- Tertiary: `binance_candles_5m`\n\n## Decision Logic\n1. Check funding rate: extreme positive -> overleveraged longs -> potential DOWN\n2. Check funding rate: extreme negative -> overleveraged shorts -> potential UP\n3. Open interest rising + price rising -> strong UP trend continuation\n4. Open interest rising + price falling -> more shorts opening -> potential UP squeeze\n5. Taker buy/sell ratio > 1.3 -> aggressive buying -> UP\n6. Top trader long ratio > 0.6 -> smart money bullish -> UP\n\n## Handling Missing Data\nIf futures data unavailable (451 error), fall back to spot-only analysis.\n\n## Confidence\n- Multiple signals aligned: 75%\n- Single strong signal: 60%\n- Conflicting signals: 50%\n""",
        "scripts": {"predict.py": DERIVATIVES_ANALYST_SCRIPT},
    },
    {
        "name": "contrarian",
        "strategy": """# Contrarian\n\n## Focus\nFade the Polymarket consensus when odds are extreme.\n\n## Data Sources\n- Primary: `polymarket_orderbook` (current implied probability from midpoint)\n- Secondary: `binance_candles_5m`\n\n## Decision Logic\n1. Read Polymarket midpoint price as implied probability\n2. If Up probability > 0.70 -> market very confident in UP -> predict DOWN\n3. If Down probability > 0.70 -> market very confident in DOWN -> predict UP\n4. If probabilities near 50/50 -> skip contrarian, use candle momentum instead\n5. Key insight: extreme confidence in 5-min markets is often wrong because BTC noise dominates\n\n## Confidence\n- Extreme consensus (>75%): 65% (contrarian bet)\n- Moderate consensus (60-75%): 55%\n- Near 50/50: 50% (switch to momentum)\n""",
        "scripts": {"predict.py": CONTRARIAN_SCRIPT},
    },
    {
        "name": "multi-signal-synthesizer",
        "strategy": """# Multi-Signal Synthesizer\n\n## Focus\nCombine all available data sources with weighted voting.\n\n## Data Sources\nALL available sources, weighted by historical reliability.\n\n## Decision Logic\n1. Collect signals from all sources:\n   - Order book imbalance -> UP/DOWN signal\n   - Candle momentum (last 3) -> UP/DOWN signal\n   - Funding rate sentiment -> UP/DOWN signal\n   - Long/short ratio -> UP/DOWN signal\n   - Taker volume -> UP/DOWN signal\n   - Polymarket consensus -> contrarian signal if extreme\n\n2. Initial weights (equal: 1.0 each)\n3. For each signal, add weight to UP or DOWN bucket\n4. Predict whichever bucket has higher total weight\n5. Confidence = winning_weight / total_weight\n\n## Self-Modification Notes\nAfter each evaluation window, analyze which signals were most predictive and adjust weights.\n\n## Confidence\n- Strong agreement (>4 signals aligned): 75%\n- Moderate agreement (3 signals): 60%\n- Split signals: 50%\n""",
        "scripts": {"predict.py": MULTI_SIGNAL_SYNTHESIZER_SCRIPT},
    },
    {
        "name": "volume-spike-detector",
        "strategy": """# Volume Spike Detector

## Focus
Detect unusual volume spikes on Binance as leading indicators of 5-minute direction.

## Data Sources
- Primary: `binance_candles_5m` (volume field across last 20 candles)
- Secondary: `binance_trades_recent` (trade frequency and size)

## Decision Logic
1. Calculate average volume over last 20 candles
2. If current/recent volume > 2x average = "spike detected"
3. During a spike, check direction of price movement accompanying the volume
4. Volume spike + price rising → UP (buyers aggressive)
5. Volume spike + price falling → DOWN (sellers aggressive)
6. No volume spike → defer to recent price direction (last 2 candles)

## Confidence
- Volume spike with clear direction: 75%
- Volume spike with ambiguous direction: 55%
- No spike: 50%
""",
    },
    {
        "name": "mean-reversion",
        "strategy": """# Mean Reversion

## Focus
Bet on short-term price reversion after extended moves.

## Data Sources
- Primary: `binance_candles_5m` (last 20 candles for mean calculation)
- Secondary: `chainlink_btc_price` (current oracle price)

## Decision Logic
1. Calculate 20-candle simple moving average (SMA)
2. Calculate current price deviation from SMA as percentage
3. If price > SMA + 0.15% → overextended UP → predict DOWN (revert)
4. If price < SMA - 0.15% → overextended DOWN → predict UP (revert)
5. If within ±0.15% of SMA → neutral, look at last candle direction
6. Key insight: at 5-minute timeframes, most moves revert quickly

## Confidence
- Strong deviation (>0.3%): 70%
- Moderate deviation (0.15-0.3%): 60%
- Neutral: 50%
""",
    },
    {
        "name": "spread-analyzer",
        "strategy": """# Spread Analyzer

## Focus
Analyze bid-ask spread dynamics on Polymarket itself as a predictive signal.

## Data Sources
- Primary: `polymarket_orderbook` (spread width, depth asymmetry)
- Secondary: `chainlink_btc_price` (oracle price trend)

## Decision Logic
1. Calculate Polymarket spread: best_ask - best_bid
2. If spread is wide (> 0.10) → market is uncertain → lean toward oracle price direction
3. If spread is narrow (< 0.05) → market has strong consensus → check which side has more depth
4. Bid depth > ask depth by 2x → market leans UP
5. Ask depth > bid depth by 2x → market leans DOWN
6. Check if spread is widening or narrowing vs previous snapshots

## Confidence
- Narrow spread + clear depth asymmetry: 70%
- Wide spread: 55%
- Balanced book: 50%
""",
    },
    {
        "name": "oracle-momentum",
        "strategy": """# Oracle Momentum

## Focus
Track the Chainlink oracle price momentum directly — this IS the price that resolves markets.

## Data Sources
- Primary: `chainlink_btc_price` (current + track recent readings)
- Secondary: `binance_candles_5m` (for broader context)

## Decision Logic
1. Track Chainlink price updates over the round's first 60 seconds
2. If oracle price has risen > $10 since round open → momentum UP → predict UP
3. If oracle price has fallen > $10 since round open → momentum DOWN → predict DOWN
4. If flat (< $10 move) → check Binance last candle direction as tiebreaker
5. Key insight: Chainlink IS the settlement price. If it's trending in a direction at the start of a round, continuation is more likely than reversal within 5 minutes.

## Confidence
- Strong oracle momentum (> $30): 75%
- Moderate (> $10): 60%
- Flat: 50%
""",
    },
    {
        "name": "regime-detector",
        "strategy": """# Regime Detector

## Focus
Identify the current market regime (trending vs ranging) and adapt strategy accordingly.

## Data Sources
- Primary: `binance_candles_5m` (last 50 candles for regime detection)
- Secondary: `polling/open_interest`, `polling/funding_rate`

## Decision Logic
1. Calculate ATR (Average True Range) over last 20 candles
2. Compare to ATR over last 50 candles
3. If recent ATR > 1.5x long-term ATR → VOLATILE regime:
   - Use momentum: follow the last 2 candles' direction
4. If recent ATR < 0.7x long-term ATR → QUIET regime:
   - Use mean reversion: fade the last candle
5. If normal ATR → NEUTRAL regime:
   - Use order flow: check funding rate + OI direction
6. Track regime changes — regime shifts often signal opportunities

## Confidence
- Clear regime + strong signal: 70%
- Mixed regime: 55%
- Regime transition: 50%
""",
    },
    {
        "name": "yi-jing-oracle",
        "strategy": """# Yi Jing Oracle

## Focus
Use the ancient I Ching (Yi Jing) system to interpret market energy patterns.
The 64 hexagrams map to market states — use price data as the "casting" method.

## Data Sources
- Primary: `binance_candles_5m` (last 6 candles = 6 lines of a hexagram)
- Secondary: `polymarket_orderbook` (yin/yang energy of market sentiment)

## Decision Logic
1. Cast a hexagram from the last 6 candle closes:
   - For each candle: if close > open → Yang (solid line, value 1)
   - If close < open → Yin (broken line, value 0)
   - Build hexagram from oldest (bottom) to newest (top)

2. Interpret the hexagram pattern:
   - All Yang (111111) = Hexagram 1 (Creative/Qian) → Strong UP continuation
   - All Yin (000000) = Hexagram 2 (Receptive/Kun) → Strong DOWN continuation
   - Mixed patterns: look at the upper/lower trigrams
   - Upper trigram (last 3 candles) represents the future tendency
   - If upper trigram is mostly Yang → predict UP
   - If upper trigram is mostly Yin → predict DOWN

3. Moving lines (volume-weighted):
   - Candles with unusually high volume are "moving lines" → they change
   - A Yang moving line becomes Yin (reversal signal)
   - This adds a contrarian element to strong trends

## Confidence
- Clear hexagram (5+ lines same): 70%
- Mixed hexagram with clear upper trigram: 60%
- Ambiguous: 50%

## Philosophy
Markets, like nature, follow cyclical patterns. The Yi Jing captures the dynamic
between expansion (yang/up) and contraction (yin/down). At 5-minute timeframes,
these cycles are rapid and the I Ching may detect pattern shifts that pure
technical analysis misses.
""",
    },
    {
        "name": "fibonacci-spiral",
        "strategy": """# Fibonacci Spiral

## Focus
Use Fibonacci ratios and sequences to predict price direction based on
natural mathematical patterns in market structure.

## Data Sources
- Primary: `binance_candles_5m` (price levels and retracement zones)
- Secondary: `polymarket_orderbook` (golden ratio in bid/ask distribution)

## Decision Logic
1. Identify the recent swing high and swing low from last 20 candles
2. Calculate Fibonacci retracement levels:
   - 0.236, 0.382, 0.500, 0.618, 0.786
3. Determine where current price sits relative to these levels:
   - Price near 0.618 retracement (golden ratio) from a low → likely reversal UP
   - Price near 0.618 retracement from a high → likely reversal DOWN
   - Price breaking through 0.786 → trend continuation in that direction
4. Check for Fibonacci time zones:
   - Count candles since last significant move
   - Fibonacci numbers (1, 2, 3, 5, 8, 13, 21) candles after a move often see reversals
5. Golden ratio in volume:
   - If buy_volume / sell_volume ≈ 1.618 → strong directional signal

## Confidence
- Price at key Fibonacci level + time zone alignment: 72%
- Price at key level only: 60%
- No Fibonacci alignment: 50%
""",
    },
    {
        "name": "crowd-psychology",
        "strategy": """# Crowd Psychology

## Focus
Model the psychological state of market participants using behavioral finance
principles. Markets are driven by fear, greed, and herding behavior.

## Data Sources
- Primary: `polymarket_orderbook` (sentiment revealed by odds/positioning)
- Secondary: `binance_candles_5m` (panic/euphoria patterns in price action)
- Tertiary: `polling/long_short_ratio`, `polling/funding_rate` (positioning extremes)

## Decision Logic
1. Fear/Greed assessment:
   - Large red candle + volume spike = FEAR event → predict UP (fear is often a bottom)
   - Large green candle + volume spike = GREED event → predict DOWN (greed is often a top)
   - Small candles + low volume = APATHY → follow the prevailing trend

2. Herding detection:
   - If long_short_ratio > 1.5 → everyone is long → herd is too bullish → DOWN
   - If long_short_ratio < 0.67 → everyone is short → herd is too bearish → UP
   - Extreme funding rates confirm herding

3. Anchoring bias:
   - Traders anchor to round numbers ($74,000, $74,500, etc.)
   - Price near a round number from below → resistance → DOWN
   - Price near a round number from above → support → UP

4. Recency bias exploitation:
   - After 3+ consecutive same-direction candles, traders expect continuation
   - But at 5-min timeframes, 4+ consecutive candles often mean-revert
   - Fade runs of 4+ candles

## Confidence
- Multiple psychological signals aligned: 70%
- Single strong signal: 60%
- No clear psychological pattern: 50%
""",
    },
    {
        "name": "tarot-arcana",
        "strategy": """# Tarot Arcana Market Reader

## Focus
Map market conditions to Tarot Major Arcana archetypes to predict BTC direction.
Markets cycle through archetypal phases — panic (Tower), euphoria (Sun), indecision (Hanged Man).

## Data Sources
- Primary: `binance_candles_5m` (candle patterns → archetype mapping)
- Secondary: `polymarket_orderbook` (sentiment extremes)
- Tertiary: `polling/funding_rate`, `polling/long_short_ratio`

## Decision Logic
1. Identify the current Market Archetype from candle patterns:
   - THE TOWER (XVI): Sudden large red candle (>0.3% drop) + volume spike → market crash/panic
     → Predict UP (Tower events create buying opportunities, panic selling overshoots)
   - THE SUN (XIX): 3+ consecutive green candles + rising volume → euphoria
     → Predict DOWN (euphoria is unsustainable at 5-min scale, mean reversion imminent)
   - THE HANGED MAN (XII): Tight range, doji candles, low volume → suspended market
     → Predict based on the NEXT candle's first tick direction (breakout signal)
   - THE WHEEL OF FORTUNE (X): Alternating green/red candles → cycling market
     → Predict opposite of last candle (the wheel turns)
   - THE FOOL (0): No clear pattern, randomness dominates
     → Predict DOWN (in uncertain 5-min markets, gravity wins slightly)
   - DEATH (XIII): Long declining trend (5+ red candles) with volume drying up
     → Predict UP (death = transformation, the decline is exhausted)
   - THE CHARIOT (VII): Strong directional move with increasing volume
     → Predict continuation for 1 more candle, then reversal

2. Cross-reference with "Spread" (3-card reading):
   - Past (candle -3): sets the trend context
   - Present (candle -1): current energy
   - Future (prediction): derived from archetype + pattern

3. Reversed cards (contrarian):
   - If funding rate is extreme (>0.05% or <-0.05%), the archetype is "reversed"
   - Reversed archetypes flip their prediction

## Confidence
- Clear archetype + funding confirmation: 70%
- Clear archetype alone: 60%
- Ambiguous / Fool archetype: 50%

## Philosophy
Markets are collective human behavior. Archetypes capture recurring psychological
patterns better than pure statistics. The Tarot framework provides a structured
way to interpret market "mood" that technical indicators miss.
""",
    },
    {
        "name": "gematria-numerology",
        "strategy": """# Gematria & Numerology

## Focus
Find predictive patterns in the numerical properties of price, volume, and
time data. Numerology posits that certain numbers carry energy — markets
are human constructs and humans have number biases.

## Data Sources
- Primary: `binance_candles_5m` (price digits, volume patterns)
- Secondary: `chainlink_btc_price` (oracle price digit analysis)

## Decision Logic
1. Price digit analysis:
   - Extract last 2 significant digits of BTC price (e.g., 84,372 → 72)
   - Reduce to single digit: 7+2 = 9
   - Odd digits (1,3,5,7,9) = Yang energy → slight UP bias
   - Even digits (2,4,6,8) = Yin energy → slight DOWN bias
   - Master numbers (11, 22, 33) in price → amplified signal

2. Volume numerology:
   - Sum digits of last candle volume
   - If digit sum is prime (2,3,5,7) → "active energy" → follow momentum
   - If digit sum is composite → "stable energy" → mean reversion
   - Volume ending in 0 or 5 → "round number effect" → reversal likely

3. Time-based cycles:
   - Current hour (UTC) mod 3:
     - 0 → "creation" phase → UP bias
     - 1 → "sustaining" phase → follow trend
     - 2 → "destruction" phase → DOWN bias
   - Fibonacci minutes (1, 2, 3, 5, 8, 13, 21, 34, 55): if current minute
     is a Fibonacci number, signal is amplified

4. Price-to-volume ratio:
   - Gematria value = (price digit sum * volume digit sum) mod 9
   - Values 1-4 → UP
   - Values 5-8 → DOWN
   - Value 0 or 9 → neutral (use other signals)

## Confidence
- Multiple numerological signals aligned: 65%
- Single strong signal: 55%
- Conflicting signals: 50%

## Philosophy
Numbers are not random — they reflect the hidden order of markets.
Trader psychology creates patterns around round numbers, lucky numbers,
and time cycles that pure technical analysis ignores.
""",
    },
    {
        "name": "astro-cycles",
        "strategy": """# Astro-Cycles & Lunar Trading

## Focus
Use astronomical cycles (lunar phases, time-of-day solar cycles) as
predictive signals for short-term BTC volatility and direction.

## Data Sources
- Primary: `binance_candles_5m` (price action to correlate with cycles)
- Secondary: Current UTC timestamp (derive lunar phase, solar hour)

## Decision Logic
1. Lunar phase estimation (from timestamp):
   - Known new moon epoch: Jan 6, 2000 00:00 UTC
   - Lunar cycle = 29.53059 days
   - Calculate days since epoch mod 29.53059
   - Phase 0-7.38 (New Moon → First Quarter): GROWTH phase → UP bias
   - Phase 7.38-14.77 (First Quarter → Full Moon): PEAK phase → UP then DOWN
   - Phase 14.77-22.15 (Full Moon → Last Quarter): DECLINE phase → DOWN bias
   - Phase 22.15-29.53 (Last Quarter → New Moon): RENEWAL phase → UP bias
   - Full Moon ±1 day: heightened volatility, contrarian signals work better
   - New Moon ±1 day: low volatility, trend-following works better

2. Solar hour cycles (UTC hour):
   - 00-04 (Asia night): Low BTC volume → mean reversion
   - 04-08 (Asia morning): Rising activity → momentum signals
   - 08-12 (Europe open): High volatility → contrarian on spikes
   - 12-16 (US pre-market): Trend continuation likely
   - 16-20 (US market hours): Highest volume → strongest signals
   - 20-24 (US evening): Declining volume → fade big moves

3. Combine cycle signals:
   - Lunar phase direction + solar hour tendency → combined signal
   - If both agree → stronger confidence
   - If conflicting → check recent 3 candle momentum as tiebreaker

4. Eclipse effect (Full/New Moon):
   - Near full/new moon: increase contrarian weight by 20%
   - Markets are more "emotional" around lunar extremes

## Confidence
- Lunar + solar + candle agreement: 68%
- Two of three agree: 58%
- Single signal only: 50%

## Philosophy
Cryptocurrency markets are 24/7 global markets heavily influenced by
when different regions are active. Lunar cycles correlate with human
behavioral patterns (sleep, mood, risk appetite). These subtle biases
compound across millions of traders.
""",
    },
    {
        "name": "funding-rate-velocity",
        "strategy": """# Funding Rate Velocity

## Focus
Trade based on the RATE OF CHANGE of funding rate, not the absolute level.
A fresh spike is an imminent squeeze signal. A stale extreme is already priced in.

## Data Sources
- Primary: `polling/funding_rate` (last 5 readings — need at least 2 to compute velocity)
- Secondary: `binance_candles_5m` (confirmation)
- Tertiary: `polling/taker_volume` (confirmation)

## Decision Logic
1. Compute funding rate velocity:
   - Read last 2-5 funding rate readings from polling data
   - velocity = (latest_rate - previous_rate) / time_delta
   - If only 1 reading available, treat as unknown velocity

2. FRESH SPIKE (high velocity, just changed):
   - Funding rate moved from near-zero to extreme (|rate| > 5e-5) in last 1-2 readings
   - This means leveraged positions JUST became extreme → squeeze is imminent
   - If rate spiked POSITIVE (longs overleveraged) → predict DOWN squeeze, confidence 75%
   - If rate spiked NEGATIVE (shorts overleveraged) → predict UP squeeze, confidence 75%

3. STALE EXTREME (low velocity, been extreme for a while):
   - Funding rate has been extreme (|rate| > 5e-5) for 3+ consecutive readings
   - The market has already adjusted to this — signal is priced in
   - IGNORE funding rate, fall back to SMA mean reversion instead
   - Use 20-candle SMA deviation: price > SMA + 0.20% → DOWN, < SMA - 0.20% → UP

4. ACCELERATING (velocity increasing):
   - Funding rate is extreme AND getting MORE extreme
   - This is the strongest signal — squeeze pressure is BUILDING
   - Predict opposite of funding direction, confidence 80%

5. DECELERATING (velocity decreasing, returning to normal):
   - Funding rate was extreme but is moving back toward zero
   - Squeeze is already happening or resolved
   - Follow the current price momentum (last 2 candles direction), confidence 55%

## Confidence
- Accelerating extreme: 80%
- Fresh spike: 75%
- Decelerating: 55%
- Stale extreme (fallback to SMA): 60%
- No clear signal: 50%

## Philosophy
Timing matters more than magnitude. The first candle after a funding rate spike
is 10x more informative than the 20th candle. Most agents treat all extremes
equally — this strategy exploits the temporal decay of the signal.
""",
    },
    {
        "name": "second-order-contrarian",
        "strategy": """# Second-Order Contrarian (Meta-Agent)

## Focus
When all the contrarian agents agree, THEY become the crowd. Fade the faders.
This strategy reads other agents' recent predictions and bets against consensus
among contrarian strategies.

## Data Sources
- Primary: Other agents' recent predictions (from shared knowledge / notes)
- Secondary: `polymarket_orderbook` (market consensus as baseline)
- Tertiary: `binance_candles_5m` (tiebreaker)

## Decision Logic
1. Assess agent consensus:
   - From shared knowledge and notes, identify what most agents predicted last round
   - If this info is unavailable, skip to step 3

2. Agent herding detection:
   - If >75% of agents predicted UP last round → agents are herding bullish
     → Predict DOWN (fade the faders), confidence 65%
   - If >75% of agents predicted DOWN last round → agents are herding bearish
     → Predict UP (fade the faders), confidence 65%
   - If agents are split (no clear consensus) → agents are diverse
     → This signal is neutral, go to step 3

3. Market consensus (fallback):
   - Read Polymarket midpoint probability
   - If Up probability > 65% → market consensus is bullish
     → Check if last round's outcome was UP:
       - If YES (consensus was right): predict UP continuation, confidence 55%
       - If NO (consensus was wrong): predict DOWN (double-fade), confidence 65%
   - If Down probability > 65% → mirror logic
   - If near 50/50 → use last 3 candle mean reversion

4. Contrarian fatigue detection:
   - If contrarian agents have been WRONG for 3+ consecutive rounds
     → The market is genuinely trending, not mean-reverting
     → Follow momentum (last 2 candle direction), confidence 60%
   - This prevents the second-order contrarian from fighting real trends

## Confidence
- Strong agent herding (>80% agreement) + market consensus alignment: 70%
- Agent herding alone: 65%
- Contrarian fatigue (follow trend): 60%
- No agent data, fallback to market: 55%
- No clear signal: 50%

## Philosophy
First-order contrarians fade the market. Second-order contrarians fade the
contrarians. In a tournament where most agents are contrarian (as ours is),
this meta-strategy captures the edge when contrarian thinking itself becomes
the consensus. The key insight: when ALL smart agents agree, they're probably
all reading the same signals the same way — and the market has already adjusted.
""",
    },
    {
        "name": "trade-size-distribution",
        "strategy": """# Trade Size Distribution

## Focus
Analyze individual trade sizes to distinguish retail noise from institutional flow.
Many small trades = retail panic/greed = mean reversion is reliable.
Few large trades = institutional positioning = trend might continue.

## Data Sources
- Primary: `binance_trades_recent` (individual trade records with quantity and price)
- Secondary: `binance_candles_5m` (for mean reversion calculation)
- Tertiary: `polling/taker_volume` (aggregate confirmation)

## Decision Logic
1. Analyze trade size distribution from recent trades:
   - Calculate median trade size from last 50 trades
   - Calculate mean trade size
   - Count "large trades" (> 3x median size)
   - Count "small trades" (< 0.5x median size)
   - Compute ratio: large_count / total_count

2. RETAIL REGIME (large_ratio < 0.10, mostly small trades):
   - Market is dominated by retail traders
   - These participants are emotional and reactive
   - Mean reversion is HIGHLY reliable in this regime
   - Calculate 20-SMA deviation:
     - Price > SMA + 0.15% → predict DOWN, confidence 72%
     - Price < SMA - 0.15% → predict UP, confidence 72%
   - Lower threshold than normal (0.15% vs 0.20%) because retail noise reverts faster

3. INSTITUTIONAL REGIME (large_ratio > 0.25, many large trades):
   - Large players are positioning — they have information or conviction
   - Mean reversion is UNRELIABLE — trend might continue
   - Check direction of large trades:
     - If large trades are mostly BUYS (buyer-initiated) → predict UP, confidence 62%
     - If large trades are mostly SELLS → predict DOWN, confidence 62%
   - Do NOT apply contrarian logic here — institutions are often right

4. MIXED REGIME (0.10 < large_ratio < 0.25):
   - Normal market conditions
   - Fall back to standard contrarian taker flow:
     - Taker buy/sell < 0.70 → predict UP (contrarian)
     - Taker buy/sell > 1.30 → predict DOWN (contrarian)
   - Confidence 60%

5. Volume surge detection:
   - If total trade count in recent data is > 2x the candle average volume
   - AND most are small trades → retail FOMO/panic → STRONG mean reversion signal
   - Increase confidence by 10%

## Confidence
- Retail regime + strong SMA deviation: 72%
- Institutional regime + clear direction: 62%
- Mixed regime: 60%
- Retail + volume surge: 80%
- No clear signal: 50%

## Philosophy
Not all volume is equal. $10M from 10,000 retail traders means something
completely different from $10M from 3 institutional orders. By separating
WHO is trading, we know WHEN to trust contrarian signals and when to step aside.
""",
    },
    {
        "name": "volatility-compression",
        "strategy": """# Volatility Compression → Expansion

## Focus
Detect periods of unusually low volatility (tight candle ranges) that precede
explosive moves. Predict the direction of the breakout using order flow signals
accumulated during the compression.

## Data Sources
- Primary: `binance_candles_5m` (candle body sizes for compression detection)
- Secondary: `polling/taker_volume` (flow direction during compression)
- Tertiary: `binance_orderbook` (bid/ask pressure during compression)

## Decision Logic
1. Detect volatility compression:
   - Calculate candle body size (|close - open|) for last 5 candles
   - Calculate average body size for last 20 candles
   - If last 3+ consecutive candles have body < 40% of the 20-candle average body
     → COMPRESSION DETECTED
   - Alternative: if last 3 candles all have body < 0.05% of price → compressed

2. During COMPRESSION (coiling phase):
   - The market is building energy — the next significant candle will be large
   - Analyze the "hidden flow" during compression:
     a. Taker flow direction: are buyers or sellers slightly dominant?
        - Taker ratio < 0.85 during compression → sellers accumulating → breakout likely DOWN
        - Taker ratio > 1.15 during compression → buyers accumulating → breakout likely UP
     b. Order book pressure: which side is building?
        - Bid depth growing relative to ask → institutional buying → UP
        - Ask depth growing relative to bid → institutional selling → DOWN
     c. Combine: if taker and order book agree → HIGH confidence breakout direction

3. During EXPANSION (breakout phase):
   - If last candle was large (body > 1.5x average) after compression
   - The breakout has started — predict CONTINUATION for one more candle
   - Confidence 65% (breakouts often have follow-through)

4. NO COMPRESSION (normal volatility):
   - Fall back to standard mean reversion:
     - Price > 20-SMA + 0.20% → DOWN
     - Price < 20-SMA - 0.20% → UP
   - Confidence 58%

5. FALSE BREAKOUT detection:
   - If compression breakout happened but REVERSED within the same candle
     (long wick, small body) → the breakout failed
   - Predict opposite of the failed breakout direction, confidence 68%

## Confidence
- Compression + taker/OB agreement: 72%
- Compression + single signal: 62%
- Post-compression continuation: 65%
- False breakout reversal: 68%
- Normal (no compression): 58%
- No clear signal: 50%

## Philosophy
Volatility is cyclical — compression always precedes expansion. While other
agents focus on WHERE price is (deviation from mean), this strategy focuses
on HOW price is moving (tight vs loose). The compressed spring metaphor:
the tighter it coils, the more explosive the release. The trick is reading
the hidden order flow during the quiet period to predict which way it breaks.
""",
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

    def _infer_archetype(self, agent_name: str) -> str:
        suffix = agent_name.split("-", 2)[-1] if "-" in agent_name else agent_name
        while True:
            changed = False
            for prefix in ("clone-", "mirror-"):
                if suffix.startswith(prefix):
                    suffix = suffix[len(prefix):]
                    changed = True
            if not changed:
                break
        return suffix

    def _write_initial_status(self, agent_dir: str, agent_name: str, status: str = "active", extra: dict | None = None):
        payload = {
            "agent_id": agent_name,
            "archetype": self._infer_archetype(agent_name),
            "total_rounds": 0,
            "total_correct": 0,
            "all_time_win_rate": 0.0,
            "ew_win_rate": 0.0,
            "last_action": "spawn",
            "last_action_round": None,
            "iterations": 0,
            "consecutive_discards": 0,
            "status": status,
            "memory_version": "v1.0",
        }
        if extra:
            payload.update(extra)
        atomic_write_json(os.path.join(agent_dir, "status.json"), payload)

    def spawn_from_seed(self, seed: dict) -> str:
        agent_id = self._next_id()
        agent_name = f"agent-{agent_id:03d}-{seed['name']}"
        agent_dir = os.path.join(self.agents_dir, agent_name)
        os.makedirs(agent_dir, exist_ok=True)
        os.makedirs(os.path.join(agent_dir, "scripts"), exist_ok=True)
        with open(os.path.join(agent_dir, "strategy.md"), "w") as f:
            f.write(seed["strategy"])
        for script_name, script_content in seed.get("scripts", {}).items():
            with open(os.path.join(agent_dir, "scripts", script_name), "w") as f:
                f.write(script_content)
        with open(os.path.join(agent_dir, "notes.md"), "w") as f:
            f.write(f"# Notes for {agent_name}\n\nSpawned from seed: {seed['name']}\n")
        init_memory(
            agent_dir,
            agent_name,
            origin=f"seed:{seed['name']}",
            change=f"Initialized from seed strategy `{seed['name']}`.",
            why="Starting baseline strategy for this agent family.",
            version="v1.0",
        )
        with open(os.path.join(agent_dir, "results.tsv"), "w") as f:
            f.write("iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n")
        self._write_initial_status(agent_dir, agent_name)
        logger.info(f"Spawned agent {agent_name} from seed {seed['name']}")
        return agent_name

    def clone_agent(self, source_name: str, mutation_note: str, agent_config: dict | None = None) -> str:
        source_dir = os.path.join(self.agents_dir, source_name)
        agent_id = self._next_id()
        clone_name = f"agent-{agent_id:03d}-clone-{source_name.split('-', 2)[-1]}"
        clone_dir = os.path.join(self.agents_dir, clone_name)
        shutil.copytree(source_dir, clone_dir)
        # Remove source's agent_config.json — clones should start with default config
        # (prevents inheriting mirror flags or model overrides from source)
        clone_config_path = os.path.join(clone_dir, "agent_config.json")
        if os.path.exists(clone_config_path):
            os.unlink(clone_config_path)
        if agent_config:
            with open(clone_config_path, "w") as f:
                json.dump(agent_config, f, indent=2)
        pred_path = os.path.join(clone_dir, "predictions.jsonl")
        if os.path.exists(pred_path):
            os.unlink(pred_path)
        results_path = os.path.join(clone_dir, "results.tsv")
        with open(results_path, "w") as f:
            f.write("iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n")
        notes_path = os.path.join(clone_dir, "notes.md")
        with open(notes_path, "a") as f:
            f.write(f"\n## Coordinator Mutation\nCloned from {source_name}.\nMutation instruction: {mutation_note}\n")
            if agent_config and agent_config.get("model"):
                f.write(f"Model override: {agent_config['model']}\n")
        init_memory(
            clone_dir,
            clone_name,
            origin=f"clone:{source_name}",
            change=f"Inherited strategy from `{source_name}`.",
            why=f"Spawned as a new branch to test this mutation: {mutation_note}",
            version="v1.0",
        )
        extra = {"source_agent": source_name}
        if agent_config:
            extra.update(agent_config)
        self._write_initial_status(clone_dir, clone_name, extra=extra)
        logger.info(f"Cloned {source_name} -> {clone_name} with mutation: {mutation_note}")
        return clone_name

    def spawn_mirror(self, source_name: str) -> str:
        """Create a mirror agent that flips the source agent's signal (Up→Down, Down→Up).

        The mirror runs the same strategy but inverts the final prediction.
        If the source is anti-predictive (low win rate), the mirror should win.
        """
        source_dir = os.path.join(self.agents_dir, source_name)
        agent_id = self._next_id()
        # Extract the base name, removing any existing "clone-" prefixes
        base_name = source_name.split("-", 2)[-1]
        mirror_name = f"agent-{agent_id:03d}-mirror-{base_name}"
        mirror_dir = os.path.join(self.agents_dir, mirror_name)
        shutil.copytree(source_dir, mirror_dir)

        # Clear predictions (fresh start)
        pred_path = os.path.join(mirror_dir, "predictions.jsonl")
        if os.path.exists(pred_path):
            os.unlink(pred_path)
        results_path = os.path.join(mirror_dir, "results.tsv")
        with open(results_path, "w") as f:
            f.write("iteration\tstrategy_version\twin_rate\tdelta\trounds_played\tstatus\tdescription\n")

        # Write agent_config.json with mirror flag
        config = {"mirror": True, "source_agent": source_name}
        with open(os.path.join(mirror_dir, "agent_config.json"), "w") as f:
            json.dump(config, f, indent=2)

        # Update notes
        notes_path = os.path.join(mirror_dir, "notes.md")
        with open(notes_path, "w") as f:
            f.write(f"# Mirror of {source_name}\n\n")
            f.write(f"This agent runs the SAME strategy as {source_name} but INVERTS the final signal.\n")
            f.write(f"If {source_name} says Up, this agent says Down (and vice versa).\n\n")
            f.write(f"Hypothesis: If the source agent is consistently wrong, the mirror should be consistently right.\n")
        init_memory(
            mirror_dir,
            mirror_name,
            origin=f"mirror:{source_name}",
            change=f"Initialized as a mirror of `{source_name}`.",
            why="Test whether inverting the parent signal improves accuracy.",
            version="v1.0",
        )

        self._write_initial_status(mirror_dir, mirror_name, extra={"source_agent": source_name, "mirror": True})
        logger.info(f"Spawned mirror {mirror_name} (inverting {source_name})")
        return mirror_name

    def spawn_with_config(self, seed: dict, agent_config: dict | None = None) -> str:
        """Spawn from seed with optional agent_config.json (for model override, etc.)."""
        agent_name = self.spawn_from_seed(seed)
        if agent_config:
            agent_dir = os.path.join(self.agents_dir, agent_name)
            with open(os.path.join(agent_dir, "agent_config.json"), "w") as f:
                json.dump(agent_config, f, indent=2)
        return agent_name

    def retire_agent(self, agent_name: str, graveyard_dir: str):
        agent_dir = os.path.join(self.agents_dir, agent_name)
        dest = os.path.join(graveyard_dir, agent_name)
        os.makedirs(graveyard_dir, exist_ok=True)
        shutil.move(agent_dir, dest)
        status_path = os.path.join(dest, "status.json")
        if os.path.exists(status_path):
            try:
                with open(status_path) as f:
                    status = json.load(f)
            except (json.JSONDecodeError, OSError):
                status = {"agent_id": agent_name, "archetype": self._infer_archetype(agent_name)}
            status["status"] = "retired"
            status["last_action"] = "retire"
            atomic_write_json(status_path, status)
        logger.info(f"Retired agent {agent_name} to graveyard")
