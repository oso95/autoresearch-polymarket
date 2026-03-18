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
