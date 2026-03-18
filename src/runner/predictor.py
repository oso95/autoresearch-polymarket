import json
import asyncio
import logging
import re

logger = logging.getLogger(__name__)

def parse_prediction_response(response: str) -> dict | None:
    try:
        data = json.loads(response)
        if "prediction" in data and data["prediction"] in ("Up", "Down"):
            return {
                "prediction": data["prediction"],
                "confidence": data.get("confidence", 0.5),
                "reasoning": data.get("reasoning", ""),
            }
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'\{[^}]*"prediction"\s*:\s*"(Up|Down)"[^}]*\}', response)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return {
                "prediction": data["prediction"],
                "confidence": data.get("confidence", 0.5),
                "reasoning": data.get("reasoning", ""),
            }
        except json.JSONDecodeError:
            pass
    lower = response.lower()
    if "predict up" in lower or "prediction: up" in lower:
        return {"prediction": "Up", "confidence": 0.5, "reasoning": response[:200]}
    if "predict down" in lower or "prediction: down" in lower:
        return {"prediction": "Down", "confidence": 0.5, "reasoning": response[:200]}
    return None


class Predictor:
    # Model tiers: use fast models for predictions, stronger for evolution
    MODEL_FAST = "haiku"     # Predictions (speed matters, 10 agents per round)
    MODEL_BALANCED = "sonnet"  # Evolution (needs reasoning but not slow)
    MODEL_STRONG = "opus"      # Tournament analysis (rare, quality matters)

    def __init__(self, timeout_seconds: int = 90, model: str = "haiku"):
        self.timeout = timeout_seconds
        self.model = model

    def build_batch_prompt(self, strategy: str, snapshots: list[dict], scripts: dict, notes: str) -> str:
        """Build a prompt for batch prediction (multiple rounds in one call)."""
        parts = [
            "You are a trading strategy agent. For EACH market snapshot below, predict whether BTC will go UP or DOWN in the next 5 minutes.",
            "",
            "## Your Strategy",
            strategy,
            "",
        ]
        if notes:
            parts.extend(["## Notes", notes, ""])
        if scripts:
            parts.append("## Your Analysis Scripts")
            for name, code in scripts.items():
                parts.extend([f"### {name}", f"```python\n{code}\n```", ""])

        parts.append(f"## Market Snapshots ({len(snapshots)} rounds)")
        parts.append("")
        for i, snap in enumerate(snapshots):
            # Compact snapshot — only include key fields to save tokens
            compact = {}
            if "binance_candles_5m" in snap:
                candles = snap["binance_candles_5m"].get("candles", [])[-10:]
                compact["candles_5m"] = [
                    {"o": c.get("open"), "h": c.get("high"), "l": c.get("low"),
                     "c": c.get("close"), "v": c.get("volume")}
                    for c in candles
                ]
            if "polymarket_orderbook" in snap:
                compact["poly_book"] = snap["polymarket_orderbook"]
            if "polling" in snap:
                polling = {}
                for k, v in snap["polling"].items():
                    if isinstance(v, dict) and "data" in v:
                        polling[k] = v["data"][-2:] if v["data"] else []
                    else:
                        polling[k] = v
                compact["polling"] = polling
            if "binance_orderbook" in snap:
                ob = snap["binance_orderbook"]
                compact["binance_ob"] = {
                    "bids": ob.get("bids", [])[:5],
                    "asks": ob.get("asks", [])[:5],
                }

            parts.append(f"### Round {i+1}")
            parts.append(f"```json\n{json.dumps(compact, separators=(',', ':'))}\n```")
            parts.append("")

        parts.extend([
            "## Instructions",
            f"Analyze each of the {len(snapshots)} rounds using your strategy.",
            "Respond with ONLY a JSON array of predictions, one per round, in order:",
            "```json",
            '[{"prediction": "Up" or "Down", "confidence": 0.0-1.0, "reasoning": "brief"}]',
            "```",
            f"The array MUST have exactly {len(snapshots)} entries, one per round.",
        ])
        return "\n".join(parts)

    def build_prompt(self, strategy: str, snapshot: dict, scripts: dict, recent_results: str, notes: str) -> str:
        parts = [
            "You are a trading strategy agent. Analyze the market data below and predict whether BTC will go UP or DOWN in the next 5 minutes.",
            "",
            "## Your Strategy",
            strategy,
            "",
        ]
        if notes:
            parts.extend(["## Notes & Coordinator Suggestions", notes, ""])
        if scripts:
            parts.append("## Your Analysis Scripts")
            for name, code in scripts.items():
                parts.extend([f"### {name}", f"```python\n{code}\n```", ""])
        if recent_results:
            parts.extend(["## Your Recent Results", recent_results, ""])
        parts.extend([
            "## Current Market Snapshot",
            f"```json\n{json.dumps(snapshot, indent=2)}\n```",
            "",
            "## Instructions",
            "1. Analyze the data using your strategy framework",
            "2. You may run any of your scripts if needed",
            "3. Make your prediction",
            "",
            "Respond with ONLY a JSON object in this exact format:",
            '```json',
            '{"prediction": "Up" or "Down", "confidence": 0.0-1.0, "reasoning": "brief explanation"}',
            '```',
        ])
        return "\n".join(parts)

    async def get_batch_predictions(self, agent_dir: str, strategy: str, snapshots: list[dict], scripts: dict, notes: str, model: str | None = None) -> list[dict | None]:
        """Get predictions for multiple rounds in a single Claude call.

        Returns a list of prediction dicts (or None for failed parses), one per snapshot.
        Much faster than calling get_prediction() N times due to reduced CLI overhead.
        """
        prompt = self.build_batch_prompt(strategy, snapshots, scripts, notes)
        use_model = model or self.model
        # Longer timeout for batch (more work per call)
        batch_timeout = min(self.timeout * 2, 300)
        try:
            cmd = ["claude", "-p", prompt, "--output-format", "text", "--model", use_model]
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=agent_dir,
                ),
                timeout=batch_timeout,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=batch_timeout)
            response = stdout.decode("utf-8").strip()
            if proc.returncode != 0:
                logger.warning(f"Batch prediction failed: {stderr.decode()[:200]}")
                return [None] * len(snapshots)

            return self._parse_batch_response(response, len(snapshots))
        except asyncio.TimeoutError:
            logger.warning(f"Batch prediction timed out after {batch_timeout}s")
            return [None] * len(snapshots)
        except Exception as e:
            logger.error(f"Batch prediction failed: {e}")
            return [None] * len(snapshots)

    def _parse_batch_response(self, response: str, expected_count: int) -> list[dict | None]:
        """Parse a batch prediction response (JSON array of predictions)."""
        # Try direct parse
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return [parse_prediction_response(json.dumps(item)) if isinstance(item, dict) else None for item in data]
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in response
        array_match = re.search(r'\[[\s\S]*\]', response)
        if array_match:
            try:
                data = json.loads(array_match.group())
                if isinstance(data, list):
                    return [parse_prediction_response(json.dumps(item)) if isinstance(item, dict) else None for item in data]
            except json.JSONDecodeError:
                pass

        # Fallback: try to find individual JSON objects
        results = []
        for match in re.finditer(r'\{[^{}]*"prediction"\s*:\s*"(Up|Down)"[^{}]*\}', response):
            try:
                results.append(parse_prediction_response(match.group()))
            except Exception:
                results.append(None)

        # Pad to expected length
        while len(results) < expected_count:
            results.append(None)

        return results[:expected_count]

    async def get_prediction(self, agent_dir: str, strategy: str, snapshot: dict, scripts: dict, recent_results: str, notes: str, model: str | None = None) -> dict | None:
        prompt = self.build_prompt(strategy, snapshot, scripts, recent_results, notes)
        use_model = model or self.model
        try:
            cmd = ["claude", "-p", prompt, "--output-format", "text", "--model", use_model]
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=agent_dir,
                ),
                timeout=self.timeout,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            response = stdout.decode("utf-8").strip()
            if proc.returncode != 0:
                logger.warning(f"Claude returned non-zero exit code: {stderr.decode()}")
                return None
            return parse_prediction_response(response)
        except asyncio.TimeoutError:
            logger.warning(f"Prediction timed out after {self.timeout}s")
            return None
        except FileNotFoundError:
            logger.error("Claude CLI not found. Ensure 'claude' is on PATH.")
            return None
        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return None
